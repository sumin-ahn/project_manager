"""묶음 리뷰 입력과 프롬프트 조립 (층: app)."""
from __future__ import annotations

from typing import Callable
from collections.abc import Mapping
from typing import NamedTuple
from pathlib import Path
from collections.abc import Sequence
from pm_engine.infra import cluster_store
from pm_engine.infra import delegate_copy_git
from pm_engine.domain import delegate_errors
from pm_engine.app import delegate_prepare
from pm_engine.app import delegate_prompt
from pm_engine.domain import delegate_rounds
from pm_engine.infra import fs
from pm_engine.infra import git
from pm_engine.domain import review_form
from pm_engine.app import reviewer_prompt
from pm_engine.infra import ticket_store



# ── 묶음 리뷰 — 리뷰 입력 · 트리 검사 · 프롬프트 조립 · 준비 ──────────────────
#
# 리뷰 단위는 묶음이다. 같은 하네스(native 서브에이전트)와 다른 하네스(교차 CLI) 두 전송이 같은
# 준비(`prepare_cluster_review`)·같은 조립(`cluster_review_prompt`)·같은 돌려받기
# (`harvest_ticket_copy`)를 쓴다. 수단은 전부 기존 것이다:
#   · 프롬프트 = `reviewer_prompt.build_prompt`(맥락 헤더 + 출력 형식 + 티켓 본문 N + 검토
#     중점 + diff).
#   · 라운드 자리 = `prepare_cluster_copy`(run-dir 1 · 티켓당 라운드 파일 1 · `base_rev` 기록).
# 리뷰 입력은 `merge-base(<통합 tip>, <묶음 브랜치>)..<묶음 브랜치>` 다 — 통합 브랜치가
# 앞서 가도 이 묶음이 만든 것만 본다. 리뷰어는 슬롯을 그대로 읽는다 — 준비는 슬롯이 묶음
# 브랜치이고 작업 트리 변경이 0 일 때만(준비 트리 조건), 돌려받기는 HEAD·작업 트리가 준비 시점과
# 같을 때만(돌려받기 트리 조건) 통과한다.


class ClusterReviewInput(NamedTuple):
    """묶음 리뷰 1회의 브랜치 좌표와 변경 파일 목록."""

    cluster: str
    members: tuple[str, ...]
    base_branch: str
    branch: str
    merge_base: str
    paths: tuple[str, ...]


class ClusterReviewSources(NamedTuple):
    """리뷰 프롬프트의 읽기 전용 입력 — 브랜치 diff·멤버 본문·검토 중점."""

    review: ClusterReviewInput
    specs: dict[str, str]
    focus: str | None


def cluster_review_input(
    home, cluster: str, *, repo: Path, git_run_fn: Callable | None = None,
) -> ClusterReviewInput:
    """리뷰 대상 diff 와 그 범위를 묶음 장부에서 해소한다.

    기준은 `merge-base(<통합 브랜치 tip>, <묶음 브랜치>)` 다 — 통합 브랜치가 그동안 앞서
    갔어도 이 묶음이 만든 변경만 리뷰 입력이 된다(흡수분은 조상이라 빠진다). 입력을 해소한
    자리에서 트리를 결속한다(준비 트리 조건 · `assert_cluster_review_tree`).
    """
    fm = cluster_store.load_cluster(home, cluster)
    if fm is None:
        raise delegate_errors.DelegateError(
            f"클러스터 장부가 없습니다: {cluster} — 리뷰 단위는 선언된 묶음입니다"
            f"(`board.py cluster new <ticket-group> --tickets <T-...> --task <task>`"
            f"(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가) 으로 먼저 "
            f"선언하세요 — 코드 트리는 명시가 필수입니다)"
        )
    branch = str(fm.get("branch") or "").strip()
    base_branch = str(fm.get("base_branch") or "").strip()
    if not branch or not base_branch:
        raise delegate_errors.DelegateError(
            f"묶음 장부에 통합 브랜치 좌표가 없습니다: {cluster} "
            f"(branch={branch or '—'} · base_branch={base_branch or '—'})"
        )
    members = tuple(cluster_store.cluster_live_members(home, cluster))
    if not members:
        raise delegate_errors.DelegateError(f"클러스터 멤버가 없습니다: {cluster}")
    merge_base = delegate_copy_git._cluster_git(
        repo, "merge-base", base_branch, branch, git_run_fn=git_run_fn,
    ).strip()
    if not merge_base:
        raise delegate_errors.DelegateError(
            f"merge-base 를 해소하지 못했습니다: {base_branch}..{branch}"
        )
    span = f"{merge_base}..{branch}"
    paths = tuple(
        path for path in delegate_copy_git._cluster_git(
            repo, "diff", "--name-only", "-z", span, git_run_fn=git_run_fn,
        ).split("\0") if path
    )
    if not paths:
        raise delegate_errors.DelegateError(
            f"리뷰할 diff 가 없습니다: {span} — 묶음 브랜치에 이 주기의 변경이 없습니다"
            "(dev 라운드 산출이 묶음 브랜치에 올라와 있는지 확인하세요)"
        )
    review = ClusterReviewInput(
        cluster, members, base_branch, branch, merge_base, paths,
    )
    assert_cluster_review_tree(review, repo=repo, git_run_fn=git_run_fn)
    return review


def assert_cluster_review_tree(
    review: ClusterReviewInput, *, repo: Path,
    git_run_fn: Callable | None = None,
) -> None:
    """리뷰 대상 트리가 묶음 브랜치이고 작업 트리 변경이 0 인지 결속한다(준비 트리 조건 · fail-closed).

    프롬프트의 비교 범위는 장부 브랜치의 `merge-base..tip` 이고, 리뷰어가 실제로 읽는 파일은
    이 트리의 내용이다. 트리가 그 브랜치가 아니거나 커밋되지 않은 변경이 있으면 둘이 서로 다른
    코드가 되어 판정 전체가 헛돈다. 브랜치 대조 seam 은 확인 커맨드 실행 트리와 같은 하나다
    (`git._cluster_current_branch`).
    """
    current = git._cluster_current_branch(str(repo))
    if current != review.branch:
        raise delegate_errors.DelegateError(
            f"리뷰 대상 트리가 묶음 브랜치가 아닙니다: {repo} "
            f"(현재 {current or '미상'} · 필요 {review.branch}) — 다른 브랜치의 파일을 "
            "리뷰 입력으로 넘기지 않습니다"
        )
    pending = delegate_copy_git._slot_pending_changes(repo, git_run_fn=git_run_fn)
    if pending:
        raise delegate_errors.DelegateError(
            f"리뷰 대상 트리에 커밋되지 않은 변경이 있습니다: {repo} — "
            + " · ".join(line.strip() for line in pending)
            + " — 구현 라운드를 돌려받았는지 확인하고(돌려받기가 커밋한다) 그 밖 변경은 "
            "커밋하거나 되돌린 뒤 다시 준비하세요"
        )


def _cluster_review_focus(home, path: Path, *, cwd: Path, ) -> str:
    """PM 검토 중점 파일을 읽는다."""
    focus_path = Path(path)
    if not focus_path.is_file():
        raise delegate_errors.DelegateError(f"--focus 파일이 없습니다: {focus_path}")
    try:
        return fs.read_text_shared(
            focus_path.resolve(), encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(f"--focus 파일 읽기 실패: {focus_path}: {exc}") from exc


def build_cluster_review_prompt(
    home, review: ClusterReviewInput, specs: Mapping[str, str], *,
    tree: Path, focus: str | None,
) -> str:
    """묶음 리뷰 프롬프트 본문 — 조립기는 추가 리뷰어 채널과 **같은 하나**다.

    구조화 블록 요구는 이 프롬프트가 소유하지 않는다: 산출 자리가 티켓별 라운드 파일이고 그
    시드가 이미 채널 골격과 다음 ID 실값을 들고 있다(요구가 두 벌이면 갈린다).
    """
    header = (
        f"## 리뷰 단위: {review.cluster} (티켓 {len(review.members)})\n\n"
        "- 준비 뒤 이 트리의 HEAD 나 작업 트리를 바꾸면 돌려받기가 거부된다(라운드 파일만 쓴다 · "
        "임시 수정은 종료 전에 되돌린다)\n"
        f"- 리뷰 입력: {review.base_branch} 와 {review.branch} 의 merge-base "
        f"({review.merge_base[:12]}) 이후 {review.branch} 변경 전부\n\n"
    )
    return header + reviewer_prompt.build_prompt(
        home, tree=tree, base=review.merge_base, target=f"브랜치 {review.branch}",
        changed_paths=review.paths,
        read_commands=[(
            "git", "-C", str(tree.resolve()), "--no-pager", "diff", "--no-ext-diff",
            "--no-color", f"{review.merge_base}..{review.branch}", "--",
        )],
        ticket_bodies=[(ticket, specs[ticket]) for ticket in review.members],
        focus=focus,
        versioned_block=False,
    )


def _cluster_member_specs(home, members: Sequence[str]) -> dict[str, str]:
    """멤버 티켓 본문 — 프롬프트에 실릴 실값(선언 순서 보존)."""
    specs: dict[str, str] = {}
    for ticket in members:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
        _status, path = found
        try:
            specs[ticket] = fs.read_text_shared(
                path, encoding="utf-8", newline="",
            )
        except (OSError, UnicodeError) as exc:
            raise delegate_errors.DelegateError(f"PM 홈 티켓 읽기 실패: {path}: {exc}") from exc
    return specs


def cluster_review_sources(
    home, cluster: str, *, repo: Path, focus_path: Path | None,
    git_run_fn: Callable | None = None,
) -> ClusterReviewSources:
    """리뷰 프롬프트 입력을 읽는다(부작용 0) — 입력 해소 안에서 준비 트리 조건을 결속한다."""
    review = cluster_review_input(home, cluster, repo=repo, git_run_fn=git_run_fn)
    specs = _cluster_member_specs(home, review.members)
    focus = (
        _cluster_review_focus(home, Path(focus_path), cwd=repo, )
        if focus_path is not None else None
    )
    return ClusterReviewSources(review, specs, focus)


def prepare_cluster_review(
    home, *, cluster: str, cwd: Path, focus_path: Path | None,
    owner_pid: int | None, reopen_ordinal: int | None = None,
    git_run_fn: Callable | None = None,
) -> tuple[ClusterReviewSources, delegate_rounds.ClusterCopyPlan]:
    """두 전송이 같이 쓰는 리뷰 준비 — 준비 트리 조건·리뷰 입력·명세·검토 중점 판독 뒤에 예약한다.

    거부는 전부 예약 앞이다(board·장부·run-dir 무변경). 예약은 `prepare_cluster_copy` 가
    멤버마다 code-reviewer 라운드를 깔고 장부 행에 `base_rev`(돌려받기 트리 조건의 기준)를 남긴다.
    `owner_pid` 는 준비 프로세스가 run 의 소유자일 때(교차 실행)만 준다.
    """
    sources = cluster_review_sources(
        home, cluster, repo=cwd, focus_path=focus_path, git_run_fn=git_run_fn,
    )
    plan = delegate_prepare.prepare_cluster_copy(
        home, cluster=cluster, role=review_form.INTERNAL_REVIEW_ROLE, cwd=cwd, owner_pid=owner_pid, reopen_ordinal=reopen_ordinal,
    )
    return sources, plan


def cluster_review_prompt(
    home, sources: ClusterReviewSources, *, tree: Path, plan: delegate_rounds.ClusterCopyPlan | None,
    adapter_roots: Sequence[str],
) -> str:
    """리뷰 프롬프트 전문 — 역할 머리말 + 좌석 안내 + 리뷰 본문(두 전송이 같은 조립).

    `plan` 이 없으면(미리보기) 좌석 안내가 빠진다 — 자리가 없는 실행은 자리를 말하지 않는다.
    """
    preamble = delegate_prompt._role_preamble(review_form.INTERNAL_REVIEW_ROLE, adapter_roots)
    note = delegate_prompt._round_copy_preamble(None, plan)
    if note is not None:
        preamble += "\n" + note
    return preamble + "\n\n" + build_cluster_review_prompt(
        home, sources.review, sources.specs, tree=tree, focus=sources.focus,
    )
