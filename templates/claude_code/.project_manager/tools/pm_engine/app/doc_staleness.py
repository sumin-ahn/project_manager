"""domain 페이지 staleness 판정 (층: app).

페이지 `repo:` 소유 checkout 의 git runner, 저장소 정체성 대조, covers 의 git pathspec 관찰 가능성,
`updated` 이후 covers 커밋 유무를 판정한다.
"""
from __future__ import annotations

import shutil
import subprocess
from contextvars import ContextVar
from pathlib import Path
from collections.abc import Callable

from pm_engine.domain import doc_pages
from pm_engine.infra import pm_paths, repo_files
from pm_engine.app import freshness_lint


# git CLI argv → (returncode, stdout). DI seam 타입(worktree_records.GitRunner 선례).
GitRunner = Callable[[list], "tuple[int, str]"]
GitRunnerFactory = Callable[[dict], "GitRunner | None"]
_UNRESOLVED_GIT_RUNNER = object()

# git log 커밋 날짜 포맷(ISO 8601·`%cI` = strict ISO·`2026-06-19T07:59:00+09:00`).
_GIT_LOG_FORMAT = "--format=%cI"
GIT_TIMEOUT_SECONDS = 120

# normalized touch checkout ↔ 페이지 소유 checkout 정체성 캐시.
# domain affected/capture는 touch×page 카테시안에 가까운 반복 매칭을 하므로 checkout별
# `rev-parse --git-common-dir` 성공 결과와 경로쌍 성공 판정은 프로세스 안에서 재사용한다.
# 실패는 현재 공개 조회 배치에서만 재사용한다. 같은 touch를 affected/gap 두 절에서 재판정하는
# capture는 한 배치로 묶어 모순을 막되, 배치 종료 뒤 다음 명시 조회는 transient 실패를 재시도한다.
# ContextVar로 라이브러리 동시 소비자의 배치 캐시가 서로 섞이지 않게 한다.
_REPOSITORY_IDENTITY_CACHE: dict[Path, tuple[Path | None, str | None]] = {}
_REPOSITORY_MATCH_CACHE: dict[tuple[Path, Path], tuple[bool | None, str | None]] = {}
_REPOSITORY_FAILURE_CACHE: ContextVar[
    dict[Path, tuple[Path | None, str | None]] | None
] = ContextVar("_REPOSITORY_FAILURE_CACHE", default=None)


# ── staleness (git 기반·covers→pathspec·fail-soft) ───────────────────────────
# 페이지 `covers` 코드가 페이지 `updated` *후* git 커밋된 적 있으면 stale = "페이지 지식이
# 코드보다 뒤처졌을 수 있다". enforcement 아닌 visibility — ⚠ 표시·lint
# advisory 만(막지 않음). 판정불가(git rc≠0·covers 빈·updated 파싱 실패)는 None("unknown"),
# git 실행 예외는 전파한다.


def _real_git_runner(cwd: Path) -> GitRunner:
    """공용 captured runner를 domain의 기존 stdout-only 정책으로 배선한다.

    반환 callable: argv(list) → (returncode, stdout). git 바이너리 부재(shutil.which)면
    (1, msg)·예외(타임아웃 등)는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부 위임·
    raise 안 함). `git -C <cwd> <argv...>` 로 항상 그 repo 에 묶는다. 엔진 관례대로
    encoding="utf-8"(한글 경로/메시지 안전). page_stale 은 stdout(커밋 날짜)만 보므로
    stderr 는 합치지 않는다(worktree_pool 의 dirty 진단 결합과 달리 여기선 깔끔한 날짜만).
    """
    return repo_files.real_git_runner(
        cwd,
        missing_binary_rc=1,
        timeout=GIT_TIMEOUT_SECONDS,
        output_mode="stdout",
        which=shutil.which,
        run=subprocess.run,
    )


def _page_owner_repo(home, page: dict) -> tuple[Path | None, str | None]:
    """페이지 ``repo:`` 채널을 소유 checkout 실경로로 해소한다.

    `freshness_lint._freshness_owner_repo`가 `self`/`upstream` 해소의 단일 진실이다. 미지원/빈 repo,
    upstream 미설정·URL·이동 등은 ``(None, reason)``으로 보존한다.

    freshness runner와 normalized touch 매칭이 이 seam을 공유한다. 후자는 반환 checkout과
    ``NormalizedRepoPath.workspace``의 git common-dir 정체성을 대조해 멀티-repo 동명
    상대경로 오매칭을 막되, 같은 repo의 다른 linked-worktree 슬롯은 허용한다.
    """
    owner = page["repo"] if "repo" in page else "self"
    owner_repo, owner_error = freshness_lint._freshness_owner_repo(pm_paths.home_at(home.root), owner)
    if owner_repo is None:
        return (None, owner_error or "페이지 repo 소유 checkout 미해소")
    try:
        return (Path(owner_repo).resolve(), None)
    except (OSError, RuntimeError) as exc:
        return (None, f"페이지 repo 소유 checkout 실경로 해소 실패: {exc}")


def _page_owner_git_runner(home, page: dict) -> GitRunner | None:
    """페이지 `repo:`를 소유 checkout runner로 해소한다(date freshness 축)."""
    owner_repo, _owner_error = _page_owner_repo(home, page)
    if owner_repo is None:
        return None
    return _real_git_runner(owner_repo)


def _repository_identity(checkout: Path) -> tuple[Path | None, str | None]:
    """checkout의 저장소 정체성인 해소된 git common-dir를 반환한다.

    linked worktree 슬롯들은 checkout 절대경로가 달라도 `--git-common-dir`를 공유한다.
    반대로 별개 저장소는 같은 상대경로 파일을 가져도 common-dir가 다르다. git 실패·빈 출력·
    비-git 디렉토리는 정체성을 추측하지 않고 `(None, reason)`으로 보수적으로 제외한다.
    git 실행 예외는 전파한다.
    성공은 checkout 실경로별 프로세스 캐시, 실패는 `_repository_query_batch`의 호출 단위
    캐시에 두어 같은 조회 안의 판정을 고정하면서 다음 명시 조회에서는 재시도한다.
    """
    try:
        resolved = Path(checkout).resolve()
    except (OSError, RuntimeError) as exc:
        return (None, f"checkout 실경로 해소 실패: {exc}")
    failure_cache = _REPOSITORY_FAILURE_CACHE.get()
    if failure_cache is not None and resolved in failure_cache:
        return failure_cache[resolved]
    if resolved in _REPOSITORY_IDENTITY_CACHE:
        return _REPOSITORY_IDENTITY_CACHE[resolved]

    def failure(reason: str) -> tuple[Path | None, str | None]:
        result = (None, reason)
        if failure_cache is not None:
            failure_cache[resolved] = result
        return result

    rc, out = _real_git_runner(resolved)(["rev-parse", "--git-common-dir"])
    common_dir_text = out.strip()
    if rc != 0 or not common_dir_text or "\n" in common_dir_text:
        reason = (
            f"git common-dir 해소 실패(rc={rc})"
            if rc != 0 else "git common-dir 출력이 비었거나 잘못됨"
        )
        return failure(reason)

    try:
        common_dir = Path(common_dir_text)
        if not common_dir.is_absolute():
            common_dir = resolved / common_dir
        common_dir = common_dir.resolve()
    except (OSError, RuntimeError) as exc:
        return failure(f"git common-dir 실경로 해소 실패: {exc}")
    if not common_dir.is_dir():
        return failure(f"git common-dir 디렉토리 부재: {common_dir}")

    result = (common_dir, None)
    _REPOSITORY_IDENTITY_CACHE[resolved] = result
    return result


def _same_repository_checkouts(
        touch_checkout: Path, page_checkout: Path) -> tuple[bool | None, str | None]:
    """두 checkout이 같은 git common-dir를 쓰는지 경로쌍 캐시로 판정한다.

    `True`/`False`는 각각 동일/별개 저장소, `None`은 어느 한쪽 git 정체성 미해소다.
    """
    try:
        touch_resolved = Path(touch_checkout).resolve()
        page_resolved = Path(page_checkout).resolve()
    except (OSError, RuntimeError) as exc:
        return (None, f"checkout 실경로 해소 실패: {exc}")
    key = (touch_resolved, page_resolved)
    if key in _REPOSITORY_MATCH_CACHE:
        return _REPOSITORY_MATCH_CACHE[key]

    touch_identity, touch_error = _repository_identity(touch_resolved)
    if touch_identity is None:
        result = (None, f"touch 저장소 정체성 미해소: {touch_error}")
    else:
        page_identity, page_error = _repository_identity(page_resolved)
        if page_identity is None:
            result = (None, f"페이지 저장소 정체성 미해소: {page_error}")
        else:
            result = (touch_identity == page_identity, None)
    if result[0] is not None:
        _REPOSITORY_MATCH_CACHE[key] = result
    return result


# object-format 별 git 빈 트리 OID (git 정의 상수). `diff <빈-트리> HEAD` = HEAD 트리 전체를
# 추가로 보여 준다 — HEAD **커밋 트리**를 `:(glob)` pathspec 으로 질의하는 수단(`git ls-tree`
# 는 pathspec magic 미지원). SHA-1 만 하드코딩하면 SHA-256 repo 서 안 맞아 diff rc≠0 → 전축 silent
# skip — `rev-parse --show-object-format` 로 알고 감지 후 맞는 OID 를 쓴다(cross-platform·
# runner 경유·`/dev/null` 미의존이라 Windows 무회귀·`git hash-object -t tree /dev/null` 동치).
_EMPTY_TREE_OID_BY_FORMAT = {
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
}


def _empty_tree_oid(runner: GitRunner) -> str | None:
    """이 repo 의 object-format 에 맞는 빈 트리 OID (SHA-1/SHA-256 중립).

    `git rev-parse --show-object-format` 로 알고 감지 → 상수 조회. 미지 포맷·감지 실패(rc≠0·
    ancient git) → None(호출부가 skip). git 실행 예외는 전파한다. repo-constant 라 covers_pathspecs 가 1회 산출."""
    rc, out = runner(["rev-parse", "--show-object-format"])
    if rc != 0:
        return None
    return _EMPTY_TREE_OID_BY_FORMAT.get(out.strip())


def _pathspec_observable(runner: GitRunner, pathspec: str, verified_at: str,
                         empty_tree: str | None) -> bool | None:
    """`pathspec`(git `:(glob)` magic 등) 이 `verified_at` **기준으로** 관찰 가능한가.

    - `True`  = **HEAD 트리**(커밋됨)에 있음 **또는** pin 이후 델타 있음(`<verified_at>..HEAD` 비지
      않음 — pin 이후 삭제/rename 도 관찰 가능한 델타). 미추적 present 는 곧 pin 이후 델타라 기존
      `<sha>..HEAD` stale 검사가 자연히 stale 로 잡는다.
    - `False` = HEAD 트리에 없음 + pin 이후 델타 0 = **pin 시점부터 이 저장소가 못 봄**. never-tracked
      (`templates/**` 류·이력 0) 이든 pin *이전* 삭제/외부 이전이든 forward-관찰가능성 0 →
      동일하게 판정 불가(unverifiable·호출부 advisory).
    - `None`  = git 오류(rc≠0·non-repo)·`empty_tree` 산출 실패 — 분류 불가(거짓 분류 없이 skip).
      git 실행 예외는 전파한다.

    **판정 축을 커밋 이력과 일관되게 HEAD 트리 기준으로 한다**
    **index(staged)** 를 포함해 — 새 경로를 stage 만 해도 present 인데 `<sha>..HEAD` 는 비어 순간
    false-green 이었다. `git diff --name-only <빈-트리> HEAD -- <pathspec>`(HEAD 커밋 트리를 `:(glob)`
    그대로 질의)로 바꿔 **staged-추가는 present 아님**(HEAD 미포함)·**staged-삭제는 present 유지**
    (HEAD 트리엔 있음)로 커밋-기준 일관. 빈 트리 OID(`empty_tree`)는 object-format 중립.

    **경계를 verified_at 에 맞춘다**: pin *이전* 삭제 경로를
    present 로 오판해 조용한 clean 이 됐다 — 이제 pin 이후 델타 0 → 부재. `verified_at` 은 호출부
    (sha 해소·선조 검증 통과분)가 넘기는 유효 anchor 라 range 가 성립한다. HEAD 트리에 있으면 pin
    델타 확인을 생략한다(short-circuit)."""
    if empty_tree is None:
        return None  # 빈 트리 OID 산출 실패 — fail-soft skip.
    rc, out = runner(["diff", "--name-only", empty_tree, "HEAD", "--", pathspec])
    if rc != 0:
        return None  # git 부재/non-repo/env 오류 — 분류 불가(거짓 분류 회피).
    if out.strip():
        return True  # HEAD 트리(커밋)에 있음 — index/staged 무관.
    # HEAD 트리에 없음 — pin 이후 델타(삭제/rename 포함) 확인. pin 이전부터 부재면 델타 0 → absent.
    rc, out = runner(["log", "-1", "--oneline", f"{verified_at}..HEAD", "--", pathspec])
    if rc != 0:
        return None  # env 오류·range 불가 — 분류 불가.
    return bool(out.strip())


def _missing_foreign_hierarchy(home, glob, covers, owner_repo: Path) -> bool:
    """foreign-owned covers가 현재 checkout에 없는 최상위 계층인지 판정한다.

    ``False``가 보수 기본값이다. 즉 parse_page가 보존한 ``repo: upstream`` 신호, 별개 git
    저장소라는 정체성, literal 최상위 디렉토리의 부재를 모두 증명한 경우만 ``True``다.
    """
    if getattr(covers, "owner_repo", None) != "upstream":
        return False
    same_repository, _identity_error = _same_repository_checkouts(home.root, owner_repo)
    if same_repository is not False:
        return False
    value = str(glob or "").strip()
    parts = value.split("/", 1)
    if len(parts) != 2:
        return False  # 최상위 파일은 "부재 계층"으로 증명할 parent가 없다.
    top_level = parts[0]
    if not top_level or "*" in top_level:
        return False  # wildcard 최상위는 어느 계층인지 확정할 수 없다.
    try:
        (home.root / top_level).stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False  # 파일시스템 판정 실패를 downstream 완화로 흡수하지 않는다.
    return False


def covers_pathspecs(home, covers, *, repo: Path | None = None,
                     git_runner: GitRunner | None = None,
                     verified_at: str) -> tuple[list[str], list[str], list[str]]:
    """covers 글롭을 **`:(glob)` magic pathspec 으로 직접** verified_at 기준 관찰가능성 분할한다.

    Returns `(present, absent, unmappable)` — 전부 **원본 글롭 문자열**(호출부 표면화·재변환용):
      - `present`    = 현재 tracked **또는** pin(`verified_at`) 이후 델타 있음 → freshness 판정 가능.
      - `absent`     = 미추적 + pin 이후 델타 0 = pin 시점부터 관찰 불가 → 판정 불가(호출부 advisory).
      - `unmappable` = **지원 안 하는 covers 문법**(비-경계 `**` 등 두 방언 의미가 다른 형태)
        → 오번역 대신 **조용히 skip 안 하고** advisory(관찰불가를 정직 보고). 빈/공백 글롭은 패턴이
        아니라 제외(코드-무관·unmappable 아님).

    부재 경로는 `git log <sha>..HEAD -- <pathspec>` 가 **"델타 없음"(빈 출력)과 구분 못 해**
    조용히 green 이 되는 사각이다 — 두-git 형상에서 `templates/**` 는 제품
    worktree 소유라 PM 홈엔 그 이력이 0 이라 그 페이지는 아무리 낡아도 영원히 green 이었다.
    **판정 기준은 git 관찰가능성·경계는 verified_at·매핑은 손실 없는 `:(glob)`**이다(codex MF3/R2/R4/R6):
      - untracked 생성물(예 `board.md`)·never-tracked(`templates/**`)·pin *이전* 삭제 경로는 pin
        이후 델타 0 → **부재**(조용한 clean/green 회피).
      - HEAD 트리(커밋)에 있으면(`git diff <빈-트리> HEAD`·index/staged 무관), verified_at *이후*
        삭제/rename 된 경로는 `<sha>..HEAD` 델타로 **존재**. **`covers_glob_pathspec` 로 원본 글롭을
        직접 넘겨**(접두사 손실 폐기·지원 문법만) `**/x.py`·`src/*.py` 도 정확 판정.

    **분류 불가**: non-repo·rc≠0 인 pathspec 은 어느 쪽에도 안 넣는다(git 실행 예외는 전파)
    (거짓 분류 회피 — unmappable 과 구분: git 오류는 skip, 형식상 매핑불가는 unmappable). `verified_at`
    = pin(호출부가 anchor 유효성 검증 후 전달·range 양끝). `git_runner` 미지정 시 `_real_git_runner(repo)`.

    ``parse_page``가 보존한 ``repo: upstream`` 페이지를 별개 downstream에서 판정할 때는, 현재
    checkout에 literal 최상위 계층 자체가 없는 absent만 advisory 대상에서 제외한다. 소유 checkout
    (동일 git 정체성), 현재 checkout에 계층이 있는 경로, 정체성/계층을 증명 못 한 경로는 종전처럼
    전수 검증한다.
    """
    repo = repo or home.root
    runner = git_runner or _real_git_runner(repo)
    empty_tree = _empty_tree_oid(runner)   # object-format 중립 빈 트리 OID·1회 산출
    present: list[str] = []
    absent: list[str] = []
    unmappable: list[str] = []
    for glob in covers:
        if not str(glob).strip():
            continue  # 빈/공백 = 패턴 아님 → 제외(코드-무관·unmappable 아님).
        spec = doc_pages.covers_glob_pathspec(glob)
        if spec is None:
            unmappable.append(glob)  # 지원 안 하는 형태(비-경계 `**`·repo 밖/절대 경로 등) — advisory.
            continue
        observable = _pathspec_observable(runner, spec, verified_at, empty_tree)
        if observable is None:
            continue  # git 오류·빈 트리 OID 산출 실패 — 거짓 분류 없이 skip(fail-soft).
        if observable:
            present.append(glob)
        elif not _missing_foreign_hierarchy(home, glob, covers, repo):
            # 다른 repo 소유 페이지의 이 checkout 비출하 계층은 여기서 영구 검증할 수 없다.
            absent.append(glob)
    return present, absent, unmappable


def page_stale(home, page: dict, *,
               git_runner: GitRunner | object | None = None) -> bool | None:
    """페이지 covers 코드가 페이지 `updated` *후* git 커밋된 적 있으면 stale.

    `True` = stale(최신 covers 커밋 날짜 > updated)·`False` = fresh(커밋이 updated 이하)·
    **`None` = 판정불가(fail-soft·unknown)**. None 이 되는 경우:
      - covers 가 비었다(코드-무관 개념 — 평가 대상 없음).
      - `updated` 파싱 실패(부재·깨짐).
      - covers 글롭이 전부 pathspec 매핑 불가(빈/공백 또는 미지원 문법 — 비-경계 `**`·절대
        경로 등·`covers_glob_pathspec` 이 None) — 좁힐 대상 없음.
      - git 호출 rc≠0 또는 빈 출력(미추적·커밋 0).
    git 실행 예외(주입 runner 예외 포함)는 전파한다.

    **원본-글롭 pathspec**(`covers_glob_pathspec`·`:(glob)` magic — sha-축과 동일 판정 기계
    재사용): git 이 `*`(세그먼트 내)·경계 `**`(슬래시 횡단)을 네이티브로 매칭한다 —
    접두사 손실(단일 `*` 가 디렉토리로 과확장·접두사-없는 글롭 통째 skip)을 없앤다. 미지원/빈
    글롭은 skip(date-축은 advisory visibility·간이 신호라 unmappable 세분화 없이 fail-soft 흡수).
    covers 의 여러 글롭은 하나의 `git log -1 -- <pathspec…>` 로 합쳐 *그 중 가장 최근* 커밋
    날짜를 본다(어느 covers 코드든 바뀌면 stale). git 은 주입 `git_runner`(DI seam·테스트
    hermetic) 또는 실 subprocess(미주입).
    """
    covers = page.get("covers") or []
    if not covers:
        return None

    updated_date = doc_pages._parse_updated_date(page.get("updated"))
    if updated_date is None:
        return None

    pathspecs = [ps for ps in (doc_pages.covers_glob_pathspec(g) for g in covers) if ps]
    if not pathspecs:
        return None

    if git_runner is _UNRESOLVED_GIT_RUNNER:
        return None
    runner = git_runner or _page_owner_git_runner(home, page)
    if runner is None:
        return None
    rc, out = runner(["log", "-1", _GIT_LOG_FORMAT, "--", *pathspecs])
    if rc != 0:
        return None

    commit_date = doc_pages._parse_commit_date(out)
    if commit_date is None:
        return None
    return commit_date > updated_date
