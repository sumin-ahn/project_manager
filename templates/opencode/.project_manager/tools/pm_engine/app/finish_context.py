"""완료 기록의 stage 선언, 영향 domain 입력 해소 (층: app)."""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from pm_engine.app import doc_affected, doc_staleness, freshness_lint
from pm_engine.domain import finish_rules
from pm_engine.infra import doc_page_store, finish_store, git, pm_paths, repo_coordinates


# ── domain 연동 (soft 알림) ──────────────────────────────────
#
# 페이지 적재·touches 매칭·stale 판정은 패키지(`doc_page_store`·`doc_affected`·`doc_staleness`)를 부른다.


def affected_domain_titles(home, ticket_id: str) -> list[tuple[str, bool | None]]:
    """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록을 돌려준다.

    각 원소 = `(title, stale)` — stale 은 `doc_staleness.page_stale`(True=낡음·False=fresh·
    None=판정불가/unknown). soft step 이 stale True 줄 앞에 ⚠ 를 단다(visibility).
    페이지 적재·매칭·stale 산출 예외는 전파한다.
    touches 부재·영향 0 → [](빈 알림). `doc_affected.pages_for_touches` 재사용(중복 매칭 0).

    **소유 repo별 git_runner 1회 생성해 공유** — 페이지 `repo:`를 `freshness_lint` 의 단일 owner
    resolver로 해소하고 checkout마다 runner를 캐시해 page_stale에 넘긴다.
    owner를 해소하지 못하면 `doc_staleness` 의 sentinel을 넘겨 기본 owner runner fallback을 막고
    stale 판정을 skip한다.
    upstream/null/미지원 owner는 unverifiable(None)로 남긴다.
    """
    touches = finish_store.get_ticket_touches(home, ticket_id)
    # touches 가 비면 매칭 0 확정 — load_pages 스캔(깨진 페이지 warning 포함) 자체를 건너뛴다.
    if not touches:
        return []

    # 접두 없는 기존 ticket은 좌표 정규화를 건너뛴다.
    prefixed = [path for path in touches if finish_rules._has_worktree_touch_prefix(path)]
    if prefixed:
        normalized: list[str] = []
        for path in touches:
            if not finish_rules._has_worktree_touch_prefix(path):
                normalized.append(path)
                continue
            try:
                normalized.append(repo_coordinates.normalize_repo_path(path, pm_root=home.root))
            except repo_coordinates.RepoCoordinateError as exc:
                print(
                    f"ticket_finish: touch {path!r} 좌표 정규화 경고 — {exc}; "
                    "이 경로는 제외",
                    file=sys.stderr,
                )
        touches = normalized
    if not touches:
        return []

    # 완료 알림도 domain affected와 같은 touches∩covers 계열이다. task stage와 별개로
    # 같은 normalizer를 거쳐
    pages = doc_affected.pages_for_touches(
        home, touches, doc_page_store.load_pages(pm_paths.domain_dir(home)))
    runners: dict[Path, object] = {}
    unresolved_runner = doc_staleness._UNRESOLVED_GIT_RUNNER

    def runner_for_page(page):
        owner = page["repo"] if "repo" in page else "self"
        owner_repo, _owner_error = freshness_lint._freshness_owner_repo(home, owner)
        if owner_repo is None:
            return unresolved_runner
        owner_repo = Path(owner_repo)
        if owner_repo not in runners:
            runners[owner_repo] = doc_staleness._real_git_runner(owner_repo)
        return runners[owner_repo]

    out: list[tuple[str, bool | None]] = []
    for page in pages:
        stale = doc_staleness.page_stale(home, page, git_runner=runner_for_page(page))
        out.append((page["title"], stale))
    return out


# ── 디자인 git stage 스코프 ─────────────────────────────────────
#
# **공유 워킹트리 mutation 은 선언된 경로만 건드린다.**
# 루트에서 `git add -A` 였다 — 멀티-PM 형상에서 다른 슬롯이 편집 중인 wiki 문서·무관 산출물을
# 통째로 쓸어담아 PM 커밋이 서로 꼬였다(Context·사용자 실사용 보고).
#
# 선언원은 둘, 그리고 **둘뿐**이다:
#   티켓 frontmatter `touches` — 사람이 적은 작업 범위 선언. 이미 있는 파서
#      (`get_ticket_touches`)를 그대로 쓴다.
#   **이 실행이 실제로 쓴 산출물** — `log/current.md`(:2단계) + board complete 가 옮긴 티켓
#      파일(:3단계·legacy 형상 한정·아래 `engine_written_paths`).
#
# 를 "엔진이 아는 wiki 산출물 디렉토리"(decisions/·domain/·ideas/…)로 넓히지 **않는다**:
# 그건 `pm_adr`·`domain`·`board idea` 가 **다른 실행** 에서 만든 것이라 이 mutation 의 선언분이
# 아니고, 디렉토리로 넘기는 순간 그 아래 **남의 미완성 편집까지 함께 stage** 되어 좁힌 척하고 안
# 좁힌 상태가 된다(codex must-fix). 그것들이 커밋에서 빠지는 건 정상이고, 빠졌다는 사실은 아래
# **잔여 loud 보고**가 알린다 — 그게 그 보고의 존재 이유다.
#
# gitignored 산출물(per-slot/per-task pm_state·leases·board.md·`.local/`)은 **스코프 산출 단계에서
# 명시적으로 걸러야 한다**(`git.git_scope_stageable`). `git add` 는 *명시 pathspec* 이 ignored 면
# rc=1 에러이기 때문이다 — 조용히 건너뛰는 건 광역 `add -A` 일 때뿐이다. 즉 스코프화가 이 성질을
# 반전시킨다. 안 걸르면 `touches` 에 gitignored 경로가 하나만 있어도
# board complete 이후 stage 가 통째로 죽고 잔여 loud 보고까지 사라진다.


def stage_scope(home, ticket_id: str, log_file: Path,
                run_git: Callable[[list[str]], tuple[int, str]], *,
                repo: Path | None = None, include_touches: bool = True,
                include_engine_outputs: bool = True,
                touches_workspace: Path | None = None,
                touches: Sequence[str] | None = None) -> finish_rules.StageScope:
    """이 완료 기록이 stage 할 pathspec (repo 상대·실제 `add` 가능한 **파일**).

    선언원 = 티켓 `touches` ∪ `engine_written_paths()`. **판정은 infra.git 의 repo-중립
    프리미티브 `git_scope_stage_pathspec` 한 벌을 재사용** 한다 — board-git 이 쓰는 바로 그
    함수다(스코프 산출·디렉토리 전개·서브모듈/미존재/미추적 제거). 복제하면 다음 사람이 한쪽만
    고친다.

    그 필터가 load-bearing 이다: 두-git 형상에서 `touches` 는 코드 worktree 경로(PM 홈엔 없음)
    를, board 분리 형상에선 서브모듈 내부 경로를 가리킬 수 있는데, 그대로 pathspec 에 넣으면
    `git add` 가 **rc=128 fatal** 로 죽어 아무것도 stage 되지 않는다.

    `touches` 를 주면 board 조회 대신 그 목록을 선언원으로 쓴다 — 두-repo 계획이 티켓 touches 를
    repo 별 몫으로 갈라 각 계획에 자기 몫만 넘기는 데 쓴다(주지 않으면 종전대로 board 조회).

    touches 좌표 정규화 실패와 staged 삭제 관측 rc≠0 은 빈 pathspec과 error 사유로 반환하고,
    호출부가 완료 기록 전에 차단하고 잔여 경로를 함께 보고한다. git scope 공급자의 예외는 전파한다.

    반환 pathspec 은 **커밋 pathspec** 이다 — add 가 받는 경로에 더해 미리 `git rm` 한 선언 삭제를
    싣는다. [4/5] 의 add 는 그중 작업 트리에 있는 파일만 받는다(add 와 커밋의 pathspec 분리).
    """
    scope_fn = git.git_scope_stage_pathspec
    repo = Path(repo) if repo is not None else home.root
    declared: list[Path] = []
    if include_touches:
        declared_touches = (list(touches) if touches is not None
                            else finish_store.get_ticket_touches(home, ticket_id))
        if touches_workspace is not None and any(
                finish_rules._has_worktree_touch_prefix(path) for path in declared_touches):
            try:
                declared_touches = repo_coordinates.normalize_repo_paths(
                    declared_touches,
                    pm_root=home.root,
                    workspace=touches_workspace,
                )
            except repo_coordinates.RepoCoordinateError as exc:
                return finish_rules.StageScope((), f"touches 좌표 정규화 실패 ({exc})")
        declared.extend(repo / touch for touch in declared_touches)
    if include_engine_outputs:
        # PM-home 산출물은 ticket code worktree 가 아니라 명시 Home(PM 홈) 에만 있다.
        declared.extend(finish_store.engine_written_paths(home, ticket_id, log_file))
    pathspec = list(scope_fn(repo, declared, run_git=run_git))
    # 미리 `git rm` 한 선언 삭제 — index 에도 작업 트리에도 없어 위 필터(add 가 받는 경로)가 뺀다.
    # add 는 그 경로를 받지 못해도(rc 128) 커밋은 기록하므로 이 스코프(커밋 pathspec)에 싣는다.
    # [4/5] 의 add 는 이 스코프 중 작업 트리에 있는 파일만 받는다. 작업 트리에 파일로 있는 선언은
    # 그런 삭제를 품을 수 없어 조회하지 않는다(디렉터리 선언은 그 아래를 본다). `--no-renames` —
    # 이름 바꿈 감지가 켜지면 `git mv` 의 원래 경로가 `R` 로 짝지어져 삭제 조회에서 빠진다.
    unfiled = [path for path in declared if not path.is_file()]
    relative = git.git_scope_pathspec(repo, unfiled) if unfiled else ()
    if relative:
        rc, out = run_git(["diff", "--cached", "--no-renames", "--name-only", "-z", "--diff-filter=D", "--",
                           *relative])
        if rc != 0:
            return finish_rules.StageScope((), f"선언 경로의 staged 삭제를 관측하지 못했다 (git diff --cached rc={rc})")
        pathspec.extend(path for path in dict.fromkeys(out.split("\0")) if path and path not in pathspec)
    return finish_rules.StageScope(tuple(pathspec), None)
