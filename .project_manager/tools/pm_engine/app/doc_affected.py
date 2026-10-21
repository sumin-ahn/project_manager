"""touches 와 domain 페이지 매칭 (층: app).

파일·디렉터리 touch 를 covers 와 대조해 영향받는 페이지와 어느 페이지도 덮지 않는 경로를 낸다.
"""
from __future__ import annotations

import contextlib
import os
import sys
from contextvars import ContextVar
from pathlib import Path

from pm_engine.domain import doc_pages
from pm_engine.infra import repo_files
from pm_engine.app import doc_staleness


_DIRECTORY_EXPANSION_CACHE: ContextVar[
    dict[tuple[Path, str], tuple[str, ...] | None] | None
] = ContextVar("_DIRECTORY_EXPANSION_CACHE", default=None)


@contextlib.contextmanager
def _repository_query_batch():
    """저장소 조회 결과를 한 공개 조회 호출 동안만 일관되게 보존한다."""
    failure_token = None
    expansion_token = None
    if doc_staleness._REPOSITORY_FAILURE_CACHE.get() is None:
        failure_token = doc_staleness._REPOSITORY_FAILURE_CACHE.set({})
    if _DIRECTORY_EXPANSION_CACHE.get() is None:
        expansion_token = _DIRECTORY_EXPANSION_CACHE.set({})
    try:
        yield
    finally:
        if expansion_token is not None:
            _DIRECTORY_EXPANSION_CACHE.reset(expansion_token)
        if failure_token is not None:
            doc_staleness._REPOSITORY_FAILURE_CACHE.reset(failure_token)


@_repository_query_batch()
def pages_for_path(
        home,
        path: str, pages: list[dict], *, warn_owner_mismatch: bool = True) -> list[dict]:
    """주어진 repo-relative 코드 경로를 covers 글롭으로 담는 페이지들을 돌려준다.

    경로 구분자를 POSIX(`/`)로 정규화해 매칭한다(Windows 백슬래시 무관). os.sep 뿐
    아니라 백슬래시도 직접 치환해 POSIX 실행 중 들어온 Windows 경로도 정규화한다. 빈
    covers 페이지(코드-무관 개념)는 어떤 경로도 매치하지 않는다. 실재 디렉토리는 gap과
    같은 전개기로 파일 목록을 얻고 파일별 covers 결과를 페이지 단위로 합친다. normalized touch는
    owner 채널뿐 아니라 검증된 workspace의 **저장소 정체성**도 소비한다. 페이지 ``repo:``
    채널을 ``freshness_lint._freshness_owner_repo``로 해소한 checkout과 workspace가 같은 git
    common-dir를 공유할 때만 매치한다. 소유 checkout/정체성 미해소·별개 저장소와 self 채널
    탈락은 advisory를 낸다.
    """
    candidates: list[str] = []
    for candidate in _files_for_directory_touch(home, path):
        norm = candidate.replace(os.sep, "/").replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        candidates.append(norm)
    # 디렉토리의 파일 순서는 열거 구현 세부다. 반환은 기존 공개 계약인 pages 순서를
    # 보존해 같은 페이지 집합이 파일명/열거 순서 변화만으로 재정렬되지 않게 한다.
    covers_matches = [
        page
        for page in pages
        if any(doc_pages._path_matches_covers(candidate, page["covers"])
               for candidate in candidates)
    ]

    owner = getattr(path, "owner", None)
    touch_repo = getattr(path, "repo", None)
    touch_workspace = getattr(path, "workspace", None)
    matches: list[dict] = []
    unresolved: list[str] = []
    mismatched: list[tuple[Path, Path]] = []
    for page in covers_matches:
        if owner is None:
            matches.append(page)
            continue
        if page.get("repo", "self") != owner:
            continue
        # Owned path인데 검증 workspace가 없으면 repo 귀속을 증명할 수 없다. 문자열 채널만으로
        # 채택하면 멀티-repo의 같은 상대경로가 다시 오매칭되므로 보수적으로 제외한다.
        if touch_workspace is None:
            unresolved.append("touch workspace 메타데이터 없음")
            continue
        page_repo, owner_error = doc_staleness._page_owner_repo(home, page)
        if page_repo is None:
            unresolved.append(owner_error or "페이지 repo 소유 checkout 미해소")
            continue
        try:
            workspace = Path(touch_workspace).resolve()
        except (OSError, RuntimeError) as exc:
            unresolved.append(f"touch workspace 실경로 해소 실패: {exc}")
            continue
        same_repository, identity_error = doc_staleness._same_repository_checkouts(
            workspace, page_repo)
        if same_repository is None:
            unresolved.append(identity_error or "저장소 정체성 미해소")
            continue
        if not same_repository:
            mismatched.append((workspace, page_repo))
            continue
        matches.append(page)
    if (
            warn_owner_mismatch
            and owner == "upstream"
            and not matches
            and any(page.get("repo", "self") == "self" for page in covers_matches)):
        print(
            f"domain: touch {str(path)!r} covers 는 일치하나 repo 채널이 self — repin 검토",
            file=sys.stderr,
        )
    if warn_owner_mismatch and unresolved:
        reasons = "; ".join(dict.fromkeys(unresolved))
        print(
            f"domain: touch {str(path)!r} repo({touch_repo!r}) covers 는 일치하나 "
            f"소유 checkout 미해소 — 매칭 제외 ({reasons})",
            file=sys.stderr,
        )
    if warn_owner_mismatch and mismatched:
        workspace, page_repo = mismatched[0]
        print(
            f"domain: touch {str(path)!r} repo({touch_repo!r}) workspace={workspace}와 "
            f"페이지 소유 checkout={page_repo}의 저장소 정체성이 다름 — 타 repo 매칭 제외",
            file=sys.stderr,
        )
    return matches


class _CoordinatePath(str):
    """알 수 없는 normalized path 타입의 재구성 실패 때 좌표를 보존하는 내부 경로."""

    def __new__(
            cls,
            value: str,
            *,
            owner: object,
            repo: object,
            workspace: object,
    ) -> "_CoordinatePath":
        obj = super().__new__(cls, value)
        obj.owner = owner
        obj.repo = repo
        obj.workspace = workspace
        return obj


def _touch_with_relative_path(touch: str, relative: str) -> str:
    """디렉토리 touch의 파일 경로에 normalized repo 좌표 메타데이터를 이어 붙인다."""
    owner = getattr(touch, "owner", None)
    repo = getattr(touch, "repo", None)
    workspace = getattr(touch, "workspace", None)
    if owner is None and repo is None and workspace is None:
        return relative
    try:
        return type(touch)(
            relative,
            owner=owner,
            repo=repo,
            workspace=workspace,
        )
    except TypeError:  # 좌표 키워드를 안 받는 str subclass — 좌표를 잃지 않고 기본형으로.
        return _CoordinatePath(
            relative,
            owner=owner,
            repo=repo,
            workspace=workspace,
        )


def _directory_touch_location(home, touch: str) -> tuple[Path, Path, str] | None:
    """실재하며 checkout 안에 있는 디렉토리 touch의 (checkout, directory, norm)."""
    workspace = getattr(touch, "workspace", None)
    checkout = Path(workspace) if workspace is not None else home.root
    norm = str(touch).replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.rstrip("/")
    if not norm:
        return None

    try:
        checkout = checkout.resolve()
        directory = (checkout / norm).resolve()
        directory.relative_to(checkout)
    except (OSError, RuntimeError, ValueError):
        return None
    if not directory.is_dir():
        return None
    return checkout, directory, norm


def _files_for_directory_touch(home, touch: str) -> list[str]:
    """실재 디렉토리 touch를 그 checkout 아래 파일 좌표들로 전개한다.

    git checkout이면 tracked + untracked(non-ignored)를 사용해 ``__pycache__`` 같은 ignore
    산출물이 gap에 섞이지 않게 한다. git이 반환한 파일형 엔트리는 dangling/directory symlink와
    gitlink도 포함해 그대로 신뢰한다. git을 쓸 수 없는 비-git 디렉토리만 실제 일반 파일을 찾는
    파일시스템 순회로 fallback한다. 파일/미해소/checkout 밖 경로는 원 touch 하나를 그대로
    보존한다. 실재하지만 비었거나 모든 파일이 gitignored인 디렉토리는 빈 목록으로 전개돼
    gap에서 사라진다.
    """
    location = _directory_touch_location(home, touch)
    if location is None:
        return [touch]
    checkout, directory, norm = location

    expansion_cache = _DIRECTORY_EXPANSION_CACHE.get()
    cache_key = (checkout, norm)
    cache_hit = expansion_cache is not None and cache_key in expansion_cache
    cached_files = expansion_cache[cache_key] if cache_hit else None
    if cache_hit and cached_files is None:
        # OSError 폴백도 성공 결과와 같은 배치 수명으로 고정한다. affected가 실패 폴백을
        # 본 뒤 gap이 즉시 재시도해 파일별 covered로 뒤집히면 둘 다 0인 모순이 재발한다.
        return [touch]
    if not cache_hit:
        try:
            # 이 seam 소비는 출하가 아니라 directory touch의 coverage 분모 전개다. 비었거나
            # 전부 ignored인 디렉토리의 0건은 정당하게 gap에서 사라지므로 출하용 0건 승격을
            # 적용하지 않는다. 원 touch 보존은 열거 실패에만 해당하며 빈 분모와 의미가 다르다.
            relative_files = repo_files.list_repo_owned_files(
                checkout,
                norm,
                mode=repo_files.OWNED,
                git_runner=doc_staleness._real_git_runner(checkout),
            )
        except OSError:
            if expansion_cache is not None:
                expansion_cache[cache_key] = None
            return [touch]
        cached_files = tuple(path.as_posix() for path in relative_files)
        if expansion_cache is not None:
            expansion_cache[cache_key] = cached_files

    expanded: list[str] = []
    reconstruction_failures = 0
    for relative in cached_files:
        candidate = _touch_with_relative_path(touch, relative)
        if type(candidate) is _CoordinatePath and type(touch) is not _CoordinatePath:
            reconstruction_failures += 1
        expanded.append(candidate)
    if reconstruction_failures:
        print(
            f"domain: directory touch {str(touch)!r}의 {reconstruction_failures}개 "
            "파일 좌표 재구성 실패 — 내부 좌표 타입으로 파일별 보존",
            file=sys.stderr,
        )
    return expanded


@_repository_query_batch()
def _uncovered_path_groups(
        home,
        touches: list[str] | None,
        pages: list[dict],
        *,
        warn_owner_mismatch: bool = True,
) -> list[tuple[str, list[str]]]:
    """touch별 ``(원 touch, uncovered 파일들)``을 돌려준다.

    전체 입력에서 gap 좌표를 dedup하되 원 touch별 그룹을 보존한다. ``cmd_capture``는
    파일이 여러 개인 디렉토리 전개 그룹만 표시 상한으로 접고, 공개 ``uncovered_paths``
    API는 그룹들을 다시 평탄화해 기존처럼 전체 목록을 반환한다.
    """
    if not touches:
        return []
    seen: set[tuple[object, object, str]] = set()
    groups: list[tuple[str, list[str]]] = []
    for touch in touches:
        if not isinstance(touch, str):
            continue
        stripped = touch.strip()
        # NormalizedRepoPath는 str subclass에 owner/repo 메타데이터를 단다. 이미 trim된
        # 좌표에서 touch.strip()을 호출하면 plain str로 강등되어 repo 귀속이 유실되므로
        # 값이 그대로면 원 객체를 보존한다. raw whitespace 입력만 plain trimmed str로 바꾼다.
        norm = touch if stripped == touch else stripped
        if not norm:
            continue
        group: list[str] = []
        for candidate in _files_for_directory_touch(home, norm):
            key = (
                getattr(candidate, "owner", None),
                getattr(candidate, "repo", None),
                str(candidate),
            )
            if key in seen:
                continue
            seen.add(key)
            if not pages_for_path(
                    home,
                    candidate, pages, warn_owner_mismatch=warn_owner_mismatch):
                group.append(candidate)
        groups.append((norm, group))
    return groups


@_repository_query_batch()
def uncovered_paths(
        home,
        touches: list[str] | None,
        pages: list[dict],
        *,
        warn_owner_mismatch: bool = True,
) -> list[str]:
    """touch 경로 중 *어느 페이지 covers 글롭에도 안 잡힌* 파일 전체를 돌려준다.

    capture(채록)의 gap 검출 — touched 코드인데 담당 domain 페이지가 없는 경로 = 후보
    신규 페이지. 실재 디렉토리 touch는 checkout의 파일 단위로 전개한 뒤 각 파일에
    `pages_for_path`를 적용해 매칭 0 인 것만 남긴다. recall도 같은 전개기를 사용하므로
    affected와 gap의 판정 단위는 항상 파일이다.
    **발견 순서 보존·dedup 키=(owner, repo, 경로)**라 같은 repo 좌표 중복만 접고, 문자열
    값이 같은 self/upstream·서로 다른 repo gap은 각각 보존한다. 비-문자열 touch·빈/공백
    경로는 방어적으로 건너뛴다(`pages_for_touches` 동형). 빈/None touches → [].

    반환값은 출력 상한과 무관한 **전체 목록**이다. 기본 ``capture`` 출력만 디렉토리 touch별
    상한을 적용하며 ``capture --all-gaps``는 이 전체 목록과 같은 정보를 표시한다.

    `pages` 미주입 시 `load_pages()`(실 domain/ 스캔·부재 시 []). domain/ 가 비면 *모든*
    touch 가 uncovered (담당 페이지 0) — 신규 clone 무영향(capture 가 gap 절을 띄움).
    """
    groups = _uncovered_path_groups(
        home,
        touches,
        pages,
        warn_owner_mismatch=warn_owner_mismatch,
    )
    return [path for _touch, paths in groups for path in paths]


@_repository_query_batch()
def pages_for_touches(home, touches: list[str] | None, pages: list[dict]) -> list[dict]:
    """ticket `touches`(파일/디렉토리 경로 목록)에 영향받는 domain 페이지들을 돌려준다.

    각 touch 경로에 `pages_for_path` 를 적용(중복 매칭 로직 금지·DRY)하고 결과를
    **union·dedup**(페이지 path 기준·발견 순서 안정)한다. 같은 페이지가 여러 touch 에
    걸려도 한 번만 담는다. 빈/None touches → `[]`.

    touch 문자열은 `strip()` 후 매칭한다(`uncovered_paths` 동형) — covers 글롭은 정확
    경로로 매치하므로 선행/후행 공백이 붙은 touch(예: 직접 API 호출 시)는 strip 없으면
    silent-miss 한다. 콤마분리 CLI 는 이미 `cmd_*` 에서 strip 되지만 직접 호출도 견고화.
    빈/공백 touch 는 건너뛴다.

    `pages` 미주입 시 `load_pages()`(실 domain/ 스캔·부재 시 []). 테스트는 hermetic
    하게 파싱된 pages 를 직접 주입해 실 디렉토리를 건드리지 않는다.
    """
    if not touches:
        return []
    seen: set[Path] = set()
    out: list[dict] = []
    for touch in touches:
        if not isinstance(touch, str):
            continue
        stripped = touch.strip()
        # uncovered_paths와 pages_for_touches가 같은 좌표/owner를 pages_for_path에 넘겨야
        # coverage gap도 타 repo 페이지로 거짓 은닉되지 않는다.
        norm = touch if stripped == touch else stripped
        if not norm:
            continue
        for page in pages_for_path(home, norm, pages):
            key = page["path"]
            if key in seen:
                continue
            seen.add(key)
            out.append(page)
    return out
