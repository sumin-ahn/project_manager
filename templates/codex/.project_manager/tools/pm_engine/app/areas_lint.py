"""repo 등록부(areas.md) lint (층: app).

미등록 안내, repo 중복 행, `merge=union` 선언 누락을 판정한다.
"""
from __future__ import annotations


from pm_engine.domain import gitattributes_rules
from pm_engine.infra import areas_store, fs, pm_paths


# 등록 0 형상(구 clone) 이관 안내 — `board.py init` 이 repo 행을 항상 등록하기 전에 만들어진
# clone 은 areas.md 에 자기 repo 행이 없다. 그 홈에선 prefix 해소가 레지스트리에서 끝나지 못하고
# (세션 유도·count-based 둘 다 입력이 없다) per-clone conf 키는 더 이상 읽지 않으므로, 옛 conf
# `prefix=` 로 네임스페이스를 받던 채택자가 조용히 무prefix(`T-NNNN`)로 바뀐다. 처방은 init
# 재실행 한 번이라 차단하지 않고 상시 가시화만 한다.
AREAS_REPO_UNREGISTERED_NOTICE = (
    "areas.md 에 이 clone 의 repo 행이 없다(등록 0 = 구 형상) — `board.py init` 을 한 번 다시 "
    "실행하면 repo 행이 등록된다(`--prefix <카테고리> --area <설명>` 을 주면 그 카테고리로, "
    "안 주면 빈 prefix 칼럼 = 무prefix `T-NNNN` 로). per-clone local.conf 의 `session=`/"
    "`prefix=` 는 더 이상 읽지 않는다 — 세션은 lease 장부, prefix 는 areas.md 가 단일 진실이다."
)


def lint_areas_repo_unregistered(home) -> list[tuple[str, str, str]]:
    """areas.md 에 등록된 repo 행이 하나도 없으면 이관 안내 advisory (never-block).

    `init` 이 repo 행을 항상 등록하므로 "등록 0" 은 그 변경 **이전에** setup 된 clone 뿐이다.
    그 형상은 깨지지 않지만(무prefix 발행은 1급 형상) 옛 per-clone conf 키에 기대던 prefix
    네임스페이스가 조용히 사라지므로, 처방(init 재실행)을 상시 보인다.

    kind=`areas-repo-unregistered`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 비기여·push
    미차단). areas 파싱 실패는 빈 결과(lint 를 깨지 않는다).
    """
    try:
        if areas_store.registered_repos(home):
            return []
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 무발화(advisory 가 lint 를 깨지 않는다).
        return []
    # 라벨은 파일명 고정 — `_rel_to_repo` 는 앵커(worktree/PM 홈)에 따라 상대/절대가 갈려
    # 같은 보드에서 다른 문자열이 나온다(앵커 무관 동일 출력 계약).
    return [("areas.md", "areas-repo-unregistered", AREAS_REPO_UNREGISTERED_NOTICE)]


def lint_areas_duplicate_repo(home) -> list[tuple[str, str, str]]:
    """areas.md 에 같은 repo 행이 2개 이상이면 advisory (never-block).

    first-match 리졸버(`_repo_protected`·`_repo_base`·`_areas_git_url`)가
    전부 첫 매칭 행에서 return 하므로 중복 행은 **조용히 first-match 로 굳는다** — 사용자가
    보는 표와 엔진이 읽는 값이 갈린다. legacy inline 형상(`merge=union` 활성)에서 두 clone 이
    같은 행을 각자 고치면 실제로 생길 수 있다. 자동 병합은 하지 않고(사람 판정) 존재만 보인다.
    `areas_set_cell`(setter)의 fail-loud 와 짝 — 그쪽은 쓰기를 막고, 이쪽은 상시 가시화한다.

    kind=`areas-duplicate-repo`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 비기여·push
    미차단). areas.md 부재/파싱 실패는 빈 결과(무영향).
    """
    try:
        _header, rows = areas_store._parse_areas(home)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 빈 결과(lint 를 깨지 않는다).
        return []
    counts: dict[str, int] = {}
    for row in rows:
        repo = row.get("repo")
        if repo:
            counts[repo] = counts.get(repo, 0) + 1
    return [
        (repo, "areas-duplicate-repo",
         f"areas.md 에 repo 행이 {n}개 — first-match 로 조용히 굳는다(어느 행이 이기는지 "
         f"merge 순서에 달림). 한 행만 남기고 수동 정리하라 "
         f"({pm_paths._rel_to_repo(home, pm_paths.areas_file(home))}). 정리 전엔 `pm-config repo protected {repo} …` "
         "같은 셀 변경이 거부된다.")
        for repo, n in sorted(counts.items()) if n > 1
    ]


def lint_areas_merge_union(home) -> list[tuple[str, str, str]]:
    """areas.md 의 `merge=union` 이 **그 파일을 담은 git** 에 선언돼 있나 (never-block).

    union 이 빠지면 두 clone 의 동시 등록이 merge 에서 한쪽 행을 잃거나 충돌한다 — 레지스트리
    설계가 기대는 보장이 조용히 사라지는 상태라 상시 가시화한다. 형상별로 *유효한* 선언 위치가
    다르므로 그에 맞는 파일만 본다(`_BOARD_AREAS_ATTR_TARGETS` / `_INLINE_AREAS_ATTR_TARGETS`):
      - board 분리 → `Home.board_root/.gitattributes` 의 `areas.md merge=union`
      - inline     → 루트 `.gitattributes` 의 `.project_manager/areas.md merge=union`
    판정은 **파일 내용**만 본다 — `git check-attr` 를 런타임에 부르지 않는다(비용·이식성).
    그래서 사각이 둘 있다(둘 다 advisory 라 무해·완전 판정은 check-attr 이 필요): in-tree
    `.gitattributes` 보다 우선하는 `.git/info/attributes`·`core.attributesFile` 은 보이지
    않는다(거기서 union 을 줬어도 여기선 미배포로 읽힌다) · glob 패턴 한계는
    `_gitattributes_merge_attr` docstring 참조.

    kind=`areas-merge-union`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 비기여·push 미차단).
    areas.md 부재(미등록)·읽기 실패는 빈 결과.
    """
    af = pm_paths.areas_file(home)
    if not af.exists():
        return []
    root = home.board_root
    separated = (root / ".git").exists() and af.parent == root
    if separated:
        attrs, targets = root / ".gitattributes", gitattributes_rules._BOARD_AREAS_ATTR_TARGETS
        fix = (f"`{pm_paths._rel_to_repo(home, attrs)}` 에 `areas.md merge=union` 을 추가하라 — 다음 board "
               "mutation(claim/new/complete 등)이 자동 보강해 그 commit 으로 함께 push 한다.")
    else:
        attrs, targets = home.root / ".gitattributes", gitattributes_rules._INLINE_AREAS_ATTR_TARGETS
        fix = (f"`{pm_paths._rel_to_repo(home, attrs)}` 에 `.project_manager/areas.md merge=union` 을 "
               "추가하라(inline 형상의 선언 위치).")
    try:
        text = fs.read_text_shared(attrs, encoding="utf-8") if attrs.is_file() else ""
    except (OSError, UnicodeError):  # 읽기 실패·비-UTF8 은 무발화(advisory 가 lint 를 깨지 않는다).
        return []
    if gitattributes_rules._areas_union_declared(text, targets):
        return []
    return [(pm_paths._rel_to_repo(home, af), "areas-merge-union",
             f"areas.md 에 merge=union 이 유효하지 않다 — 동시 등록(다른 clone 의 행 append)이 "
             f"merge 에서 충돌하거나 한쪽 행을 잃는다. {fix}")]
