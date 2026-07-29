#!/usr/bin/env python3
"""domain 레이어 — 페이지 covers 파서 + 코드↔페이지 인덱스 (Phase 1 #1).

`domain/` 의 각 페이지 frontmatter `covers:`(담당 코드 글롭)를 파싱해 *페이지 ↔ 코드*
인덱스를 만든다. 이게 이후 touches∩covers 매칭(#2)·staleness(#3)·소환(Phase 2)의 토대다.

범위: 파서(`parse_page`)·스캔(`load_pages`)·매칭(`pages_for_path`)·touches∩covers
(`pages_for_touches`·#2)·staleness(`page_stale`·#3)·freshness lint(`lint_pages`)·
CLI(`list`/`affected`/`capture`/`capture-draft`/`lint`). capture(채록·`uncovered_paths`
gap·Phase 3)는 surface-only — 담당 페이지·coverage gap 을 *띄울* 뿐 본문 자동 생성/스탬프는
안 한다(자동 `updated:` 는 stale 탐지를 거짓으로 만듦). capture-draft(
Phase 2)는 researcher 조사 prose 를 domain 초안(`status: draft`)으로 *scaffold* 한다 —
prose 는 verbatim 배치(요약/구조화 금지)·**git 무조작**(add/commit 0)·promote(draft→정식)는
사람 손. 범위 밖(후속): derive(코드서 자동 채록·`derived:true`·Phase 5)·contradiction(LLM).

설계:
  - **frontmatter 파싱은 board.load_ticket 재사용** — 이름은 ticket 이나 임의 frontmatter md
    파서다(board.py:714). 중복 파서 정의 금지(DRY·codex reuse 강조). board 는 같은 tools/
    에서 `_load_module`(spec_from_file_location) 로 로드한다 — 패키지 설치 없이 동작하는
    board.py·pm_*.py 와 같은 로드 관례.
  - **covers 글롭 시맨틱**: anchored full-match(경로 전체) · `**`=0+ 세그먼트 재귀 ·
    `*`=한 세그먼트 내. 빈 covers=코드-무관 개념(매칭 0). `fnmatch` 단독은 `**` 가
    부정확하므로 작은 glob→regex 변환(stdlib re)으로 `**`→세그먼트 횡단·`*`→세그먼트 내.
  - **graceful**: domain/ 부재·빈 디렉토리·frontmatter 깨진/없는 페이지 → 빈 리스트/skip
    (stderr 경고·crash 0). solo·신규 clone 무영향.
  - 모듈 구조 = worktree_pool.py·pm_config.py 따름(경로 상수·argparse main·hermetic 주입
    — load_pages(domain_dir=...) 로 테스트가 tmp dir 주입 가능).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py·pm_config.py 와 동일
# 앵커 관례(어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / ".project_manager" / "tools"
DOMAIN_DIR = REPO / ".project_manager" / "wiki" / "domain"

# load_pages 가 스킵하는 비-페이지 파일(인덱스 README·복사용 템플릿).
_NON_PAGE_FILES = frozenset({"README.md", "_template.md"})

# domain lint oversized 임계 — 본문 라인수가 이 값을 넘으면 advisory finding(상수·비차단).
OVERSIZED_LINES = 200

# capture에서 디렉토리 touch 하나가 표시하는 gap 상한. 전체 gap API는 자르지 않고,
# CLI 기본 출력만 접는다(`capture --all-gaps`로 전체 표시).
DIRECTORY_GAP_DISPLAY_LIMIT = 15

# capture-draft scaffold 기본값 — frontmatter status 진실(draft 제외 기준)·type 기본.
DRAFT_STATUS = "draft"
DEFAULT_DRAFT_TYPE = "research"
# capture-draft 출력 파일 suffix(`.draft.md`) — 사람 가독 보조. **필터 기준 아님**
# (index 제외는 frontmatter `status: draft`). suffix 는 PM 가 promote 시 `.md` 로 rename.
DRAFT_SUFFIX = ".draft.md"

# source: 가 repo 밖/일시경로/stdin/미지정일 때 박는 자유서술 placeholder(절대경로 박제 금지).
# 기존 frontmatter 자유서술 토큰 관례(`<!-- TODO PM: ... -->`)와 정합 — promote 전 PM 손.
SOURCE_TODO_PLACEHOLDER = "<!-- TODO PM: 출처 -->"

# git CLI argv → (returncode, stdout). DI seam 타입(worktree_pool.GitRunner 선례).
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


@contextlib.contextmanager
def _repository_query_batch():
    """저장소 정체성 실패를 한 공개 조회 호출 동안만 일관되게 보존한다."""
    if _REPOSITORY_FAILURE_CACHE.get() is not None:
        yield
        return
    token = _REPOSITORY_FAILURE_CACHE.set({})
    try:
        yield
    finally:
        _REPOSITORY_FAILURE_CACHE.reset(token)


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.5.0"

# rev 스탬프를 지닌 형제 파일만 대조 대상. 계측 확대 시 여기 추가.
_STAMPED_SIBLINGS = frozenset({"board.py", "repo_coordinates.py"})


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    불일치/부재(구형 형제는 리터럴 부재=None)면 사본 skew → 명시 에러(어느 파일이 어떤 rev 인지
    지목 + pm-update 안내). self-contained(engine_rev.py 런타임 의존 0)라 부분복사도 정확 검출한다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (fail-soft 로더 재-raise 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


# ── 엔진 모듈 동적 로드 (스크립트-위치 앵커·board.py·pm_config.py 선례) ──────────
# board.py 는 같은 tools/ 에 있다. spec_from_file_location 으로 로드한다 — 패키지 설치
# 없이 동작(board.py·pm_*.py 와 같은 로드 관례). 부재/실패는 명시 에러로 보고한다.


def _load_module(name: str, filename: str):
    """tools/<filename> 를 모듈로 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    path = TOOLS_DIR / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 — 로드 실패는 호출부가 명시 에러로 보고.
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    if filename in _STAMPED_SIBLINGS:  # 불변식: stamped sibling 로드 지점은 verify
        _verify_engine_rev(mod, filename)
    return mod


def _load_board():
    """board 모듈을 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    return _load_module("board", "board.py")


def _load_repo_coordinates():
    """공용 repo 좌표 normalizer를 로드한다. 부재/손상은 호출부가 fail-loud 한다."""
    return _load_module("repo_coordinates", "repo_coordinates.py")


def _load_repo_owned_files():
    """공용 repo 소유 파일 열거 seam을 로드한다. 부재/손상은 호출부가 fail-loud 한다."""
    path = TOOLS_DIR / "repo_owned_files.py"
    if not path.exists():
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
        )
    module_name = f"_project_manager_repo_owned_files:{path.resolve()}"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("module spec/loader 부재")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
        ) from exc
    return module


_WORKTREE_TOUCH_PREFIX = re.compile(r"^work/[^/]+_\d+(?:/|$)")


def _has_worktree_touch_prefix(path: str) -> bool:
    """normalizer를 로드하지 않고도 접두 사용 여부만 표기 변형에 견고하게 판정한다."""
    norm = path.replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return _WORKTREE_TOUCH_PREFIX.match(norm) is not None


def _normalize_ticket_touches(touches: list[str]) -> list[str]:
    """ticket PM-home 좌표를 경로별 advisory 경고와 함께 정규화한다.

    접두가 하나도 없으면 모듈을 아예 로드하지 않아 부분 동기 adopter의 기존 repo-relative
    동작을 보존한다. 조회 표면은 한 경로 오류가 배치를 죽이지 않는다. 오류 경로는 loud
    경고 후 제외하고 나머지를 계속 처리한다(검증 없이 strip하거나 원문을 하류에 넘기지 않음).
    """
    if not any(_has_worktree_touch_prefix(path) for path in touches):
        return touches
    coords = _load_repo_coordinates()
    if coords is None:
        for path in touches:
            if _has_worktree_touch_prefix(path):
                print(
                    f"domain: touch {path!r} 좌표 정규화 경고 — repo 좌표 normalizer "
                    f"로드 실패 ({TOOLS_DIR / 'repo_coordinates.py'}); 이 경로는 제외",
                    file=sys.stderr,
                )
        return [path for path in touches if not _has_worktree_touch_prefix(path)]

    error_type = getattr(coords, "RepoCoordinateError", RuntimeError)
    normalized: list[str] = []
    for path in touches:
        if not _has_worktree_touch_prefix(path):
            normalized.append(path)
            continue
        try:
            normalized.append(coords.normalize_repo_path(path, pm_root=REPO))
        except error_type as exc:
            print(
                f"domain: touch {path!r} 좌표 정규화 경고 — {exc}; 이 경로는 제외",
                file=sys.stderr,
            )
    return normalized


# ── covers 글롭 매칭 (작은 glob→regex·stdlib) ────────────────────────────────


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """covers 글롭을 anchored full-match regex 로 변환한다.

    `**` = 0+ 세그먼트 재귀(슬래시 횡단) · `*` = 한 세그먼트 내(`[^/]*`) ·
    그 외 문자는 리터럴(`re.escape`). fnmatch 단독은 `**` 가 부정확하니 직접 변환한다.
    `src/analysis/**` ⊇ `src/analysis/factor_beta.py`, ⊉ `src/core/x.py`.

    `**` 는 **0+ 세그먼트**라 인접 슬래시를 함께 흡수한다(segment-aware):
      - trailing `/**`   → `(?:/.*)?`  : `src/analysis/**` 가 `src/analysis`(디렉토리 자체)·하위 모두 매치
      - leading/middle `**/` → `(?:.*/)?` : `src/**/x.py` 가 `src/x.py`(0 세그먼트)·`src/a/x.py` 매치
    그래서 0-세그먼트도 매치된다(`**/x.py` ⊇ `x.py`). escape 는 보존되어 `a.b/**` 의
    `.` 가 임의 문자로 새지 않는다.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**` = 0+ 세그먼트. 인접 슬래시를 흡수해 0-세그먼트도 매치.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")  # leading/middle `**/` → 0+ 세그먼트(+슬래시)
                    i += 3
                elif out and out[-1] == "/":
                    out[-1] = "(?:/.*)?"     # trailing `/**` → 0+ 세그먼트(+선행 슬래시)
                    i += 2
                else:
                    out.append(".*")         # 그 외 단독 `**` → 세그먼트 횡단
                    i += 2
            else:
                out.append("[^/]*")  # `*` → 한 세그먼트 내(슬래시 제외)
                i += 1
        elif ch == "/":
            out.append("/")          # 슬래시는 리터럴(trailing `/**` 흡수 위해 escape 안 함)
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _path_matches_covers(path: str, covers: list[str]) -> bool:
    """repo-relative 코드 경로가 covers 글롭 중 하나라도 anchored full-match 하면 True."""
    return any(_glob_to_regex(glob).match(path) for glob in covers)


def _path_or_directory_matches_covers(path: str, covers: list[str]) -> bool:
    """파일 exact/glob 또는 디렉토리 touch 아래 covers를 advisory 매치한다.

    tickets의 지배적 실형태는 ``.project_manager/tools``·``tests`` 같은 디렉토리
    선언이다. covers 원문이 ``<touch>/`` 아래에서 시작하면 그 하위 파일/글롭을 담당하는
    페이지로 소환한다. affected/capture는 soft 표면이라 확장자 없는 파일을 디렉토리로
    과대 해석할 가능성보다 recall 0 방지가 우선이다.
    """
    if _path_matches_covers(path, covers):
        return True
    directory = path.rstrip("/")
    if not directory:
        return False
    prefix = directory + "/"
    for glob in covers:
        normalized_glob = glob.replace(os.sep, "/").replace("\\", "/")
        while normalized_glob.startswith("./"):
            normalized_glob = normalized_glob[2:]
        if normalized_glob.startswith(prefix):
            return True
    return False


# ── staleness (git 기반·covers→pathspec·fail-soft) ───────────────────────────
# 페이지 `covers` 코드가 페이지 `updated` *후* git 커밋된 적 있으면 stale = "페이지 지식이
# 코드보다 뒤처졌을 수 있다". enforcement 아닌 visibility — ⚠ 표시·lint
# advisory 만(막지 않음). 판정불가는 전부 None(fail-soft·"unknown") — git 부재(솔로/CI)·
# 에러·covers 빈·updated 파싱 실패에 crash 0.


def _real_git_runner(cwd: Path) -> GitRunner:
    """실 git 을 `cwd` 컨텍스트로 호출하는 GitRunner (worktree_pool._real_git_runner 패턴).

    반환 callable: argv(list) → (returncode, stdout). git 바이너리 부재(shutil.which)면
    (1, msg)·예외(타임아웃 등)는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부 위임·
    raise 안 함). `git -C <cwd> <argv...>` 로 항상 그 repo 에 묶는다. 엔진 관례대로
    encoding="utf-8"(한글 경로/메시지 안전). page_stale 은 stdout(커밋 날짜)만 보므로
    stderr 는 합치지 않는다(worktree_pool 의 dirty 진단 결합과 달리 여기선 깔끔한 날짜만).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
            )
            return result.returncode, result.stdout or ""
        except Exception as exc:  # noqa: BLE001 — fail-soft: 타임아웃/예외를 rc!=0 로 surface.
            return 1, str(exc)

    return runner


def _page_owner_repo(page: dict) -> tuple[Path | None, str | None]:
    """페이지 ``repo:`` 채널을 소유 checkout 실경로로 해소한다.

    board의 `_freshness_owner_repo`가 `self`/`upstream` 해소의 단일 진실이다. 미지원/빈 repo,
    upstream 미설정·URL·이동 등은 ``(None, reason)``으로 보존한다. 구 board(리졸버 부재)와
    board 로드 불가는 키 부재/명시 self만 기존 self checkout으로 자연 퇴화한다.

    freshness runner와 normalized touch 매칭이 이 seam을 공유한다. 후자는 반환 checkout과
    ``NormalizedRepoPath.workspace``의 git common-dir 정체성을 대조해 멀티-repo 동명
    상대경로 오매칭을 막되, 같은 repo의 다른 linked-worktree 슬롯은 허용한다.
    """
    board = _load_board()
    resolver = getattr(board, "_freshness_owner_repo", None) if board is not None else None
    if resolver is None:
        # 구 board/로드 불가에서도 `upstream`을 self로 흡수하지 않는다. 키 부재/명시 self만
        # legacy self checkout으로 퇴화하고 그 밖의 값은 unknown(None).
        owner = page.get("repo", "self")
        if not isinstance(owner, str) or owner.strip() != "self":
            return (None, "페이지 repo 소유 checkout resolver 부재")
        try:
            return (REPO.resolve(), None)
        except (OSError, RuntimeError) as exc:
            return (None, f"self 소유 checkout 해소 실패: {exc}")
    try:
        owner = page["repo"] if "repo" in page else "self"
        owner_repo, owner_error = resolver(owner)
    except Exception as exc:  # noqa: BLE001 — owner 해소 실패는 stale unknown.
        return (None, f"페이지 repo 소유 checkout 해소 예외: {exc}")
    if owner_repo is None:
        return (None, owner_error or "페이지 repo 소유 checkout 미해소")
    try:
        return (Path(owner_repo).resolve(), None)
    except (OSError, RuntimeError) as exc:
        return (None, f"페이지 repo 소유 checkout 실경로 해소 실패: {exc}")


def _page_owner_git_runner(page: dict) -> GitRunner | None:
    """페이지 `repo:`를 소유 checkout runner로 해소한다(date freshness 축)."""
    owner_repo, _owner_error = _page_owner_repo(page)
    if owner_repo is None:
        return None
    return _real_git_runner(owner_repo)


def _repository_identity(checkout: Path) -> tuple[Path | None, str | None]:
    """checkout의 저장소 정체성인 해소된 git common-dir를 반환한다.

    linked worktree 슬롯들은 checkout 절대경로가 달라도 `--git-common-dir`를 공유한다.
    반대로 별개 저장소는 같은 상대경로 파일을 가져도 common-dir가 다르다. git 실패·빈 출력·
    비-git 디렉토리는 정체성을 추측하지 않고 `(None, reason)`으로 보수적으로 제외한다.
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

    try:
        rc, out = _real_git_runner(resolved)(["rev-parse", "--git-common-dir"])
    except Exception as exc:  # noqa: BLE001 — 주입 runner 예외도 fail-closed.
        return failure(f"git common-dir 호출 예외: {exc}")
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


def _escape_glob_literals(g: str) -> str:
    """git `:(glob)` wildmatch 특수문자 중 **우리 covers 시맨틱이 와일드카드로 안 쓰는** 것을
    백슬래시 이스케이프한다 — `*`/`**` 만 와일드카드로 보존한다.

    우리 covers 매처는 `*`(세그먼트 내)·`**`(재귀)만 와일드카드로 보고 `?`·`[`·`]` 는 리터럴로
    본다. 그런데 git `:(glob)` 은 `?`(임의 1자)·`[...]`(문자클래스)도 와일드카드라, 리터럴
    특수문자가 든 covers 경로가 다른 파일에 오매칭(거짓 stale)되거나 실제 변경을 놓친다(false-green).
    `?`·`[`·`]` 와 백슬래시(이스케이프 문자 자체)를 백슬래시로 이스케이프해 리터럴로 만든다 —
    백슬래시를 **먼저** 처리해 중복 이스케이프를 피한다. `*` 는 손대지 않는다(우리 와일드카드).
    git 이 이스케이프된 `?`/`[`/`]` 를 리터럴로 처리함은 실측 확인."""
    g = g.replace("\\", "\\\\")   # 백슬래시 자체 먼저(뒤에 추가할 이스케이프와 중복 회피)
    for ch in "?[]":
        g = g.replace(ch, "\\" + ch)
    return g


def _is_supported_covers_glob(g: str) -> bool:
    """`g`(비-공백 stripped 글롭)가 **우리 matcher 와 git `:(glob)` 이 동일 의미인** 형태인가.

    엣지별 추격 대신 **지원 문법을 명시 검증**하고, 두 방언에서 의미가 증명된 형태만 변환한다.
    지원(실 git property 테스트로 동일성 증명·`tests/test_domain_freshness.py`):
      - 와일드카드 없는 **정확 경로**(파일 지목 — git 에서 dir 이면 하위 subtree 매칭돼 우리 exact
        매치보다 넓다·freshness 는 over-warn 이라 안전; 실사용=파일).
      - 단일 `*`(세그먼트 내 `[^/]*`) — `dir/*`·`dir/*.ext`·중간 `*` 어디든.
      - leading `**/`·middle `/**/` 경계 `**`, **리터럴-prefix** trailing `/**`(`src/**`·`src/nested/**`).
    **미지원**(→ 호출부 unmappable advisory·오번역 안 함): **비-경계 `**`**(`**.py`·`src/**.py`·`a**b`
    — git 은 `**` 를 세그먼트 못 넘게 처리해 중첩 파일을 **miss=false-green**), `***` 이상,
    **repo-밖/절대 경로**(선행 `/`·`..` 세그먼트·Windows 드라이브 `X:`),
    **wildcard-prefix + trailing `/**`**(`*/**`·`a*b/**`·`src/*/**`·`*/literal/**` — 우리 matcher 의
    trailing `/**`=parent-포함(`(?:/.*)?`)이 prefix-레벨/루트 파일(`.gitignore`)까지 매칭하나 git
    `:(glob)…/**` 는 슬래시 필수라 제외 → **git miss=false-green**). 우리 실사용(전 페이지 =
    정확 경로 + 리터럴-prefix `/**`)은 전부 지원 집합 안(실 git property 테스트)."""
    # repo-밖/절대 경로 선분류 (순수 문자열·git 호출 불요).
    if g.startswith("/"):
        return False  # 절대 경로(POSIX)
    if re.match(r"[A-Za-z]:", g):
        return False  # Windows 드라이브(`C:/`·`C:\`)
    if ".." in re.split(r"[\\/]", g):
        return False  # `..` 세그먼트 = repo 밖 탈출
    # trailing `/**` 는 prefix 가 전부 리터럴일 때만 두 방언 동등 — prefix 에 와일드카드가 있으면
    # 우리 parent-포함 매칭(prefix-레벨/루트 파일)이 git `…/**`(슬래시 필수)와 갈린다.
    if g.endswith("/**") and "*" in g[:-3]:
        return False
    i, n = 0, len(g)
    while i < n:
        if g[i] != "*":
            i += 1
            continue
        j = i
        while j < n and g[j] == "*":
            j += 1
        run = j - i
        if run == 1:
            i = j  # 단일 `*` — 세그먼트 내(`[^/]*`)·두 방언 동등.
        elif run == 2:
            # `**` — 양쪽이 `/`(또는 문자열 경계)일 때만 두 방언 동등(경계 `**`).
            left_boundary = (i == 0) or (g[i - 1] == "/")
            right_boundary = (j == n) or (g[j] == "/")
            if not (left_boundary and right_boundary):
                return False  # 비-경계 `**` — git 이 세그먼트 못 넘어 miss(false-green).
            i = j
        else:
            return False  # `***` 이상 — 미지원.
    return True


def covers_glob_pathspec(glob) -> str | None:
    """covers 글롭을 git **`:(glob)` magic pathspec** 으로 직접 변환한다 (손실 접두사 폐기).

    `git diff`(HEAD 트리)/`git log` 에 `:(glob)<원본 글롭>` 을 넘겨 git 이 `*`(세그먼트 내·`/` 안 넘음)·
    경계 `**`(슬래시 횡단)을 **네이티브로** 처리하게 한다 — 접두사 추출의 **손실**(접두사-없는 글롭
    통째 skip·`src/*.py`→`src/` 과확장)을 없앤다.
    `*` 외 git glob 특수문자(`?`·`[`·`]`·백슬래시)는 `_escape_glob_literals` 로 이스케이프해 리터럴
    보존. **지원 문법만 변환**한다(`_is_supported_covers_glob`) — 빈/공백 또는 두
    방언 의미가 다른 형태(비-경계 `**` 등)는 오번역 대신 `None`(호출부가 unmappable advisory).
    sha-축(`covers_pathspecs`)과 date-축(`page_stale`/`lint_domain`) 이 **공유**하는 단일 판정 기계다
    (date-축도 종전 손실 접두사에서 이 원본-글롭 판정으로 통일)."""
    g = str(glob or "").strip()
    if not g or not _is_supported_covers_glob(g):
        return None
    return f":(glob){_escape_glob_literals(g)}"


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
    ancient git·예외) → None(호출부가 fail-soft skip). repo-constant 라 covers_pathspecs 가 1회 산출."""
    try:
        rc, out = runner(["rev-parse", "--show-object-format"])
    except Exception:  # noqa: BLE001 — fail-soft.
        return None
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
    - `None`  = git 오류(rc≠0·non-repo·예외)·`empty_tree` 산출 실패 — 분류 불가(거짓 분류 없이 skip).

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
    try:
        rc, out = runner(["diff", "--name-only", empty_tree, "HEAD", "--", pathspec])
    except Exception:  # noqa: BLE001 — 주입 runner raise 도 분류 불가(skip·fail-soft).
        return None
    if rc != 0:
        return None  # git 부재/non-repo/env 오류 — 분류 불가(거짓 분류 회피).
    if out.strip():
        return True  # HEAD 트리(커밋)에 있음 — index/staged 무관.
    # HEAD 트리에 없음 — pin 이후 델타(삭제/rename 포함) 확인. pin 이전부터 부재면 델타 0 → absent.
    try:
        rc, out = runner(["log", "-1", "--oneline", f"{verified_at}..HEAD", "--", pathspec])
    except Exception:  # noqa: BLE001 — fail-soft.
        return None
    if rc != 0:
        return None  # env 오류·range 불가 — 분류 불가.
    return bool(out.strip())


def covers_pathspecs(covers, *, repo: Path | None = None,
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
    조용히 green 이 되는 사각이다 — 두-git 형상에서 `templates/**` 는 ①(제품
    worktree) 소유라 ②(PM 홈)엔 그 이력이 0 이라 그 페이지는 아무리 낡아도 영원히 green 이었다.
    **판정 기준은 git 관찰가능성·경계는 verified_at·매핑은 손실 없는 `:(glob)`**이다(codex MF3/R2/R4/R6):
      - untracked 생성물(예 `board.md`)·never-tracked(`templates/**`)·pin *이전* 삭제 경로는 pin
        이후 델타 0 → **부재**(조용한 clean/green 회피).
      - HEAD 트리(커밋)에 있으면(`git diff <빈-트리> HEAD`·index/staged 무관), verified_at *이후*
        삭제/rename 된 경로는 `<sha>..HEAD` 델타로 **존재**. **`covers_glob_pathspec` 로 원본 글롭을
        직접 넘겨**(접두사 손실 폐기·지원 문법만) `**/x.py`·`src/*.py` 도 정확 판정.

    **fail-soft**: git 부재·non-repo·rc≠0·예외인 pathspec 은 분류 불가라 어느 쪽에도 안 넣는다
    (거짓 분류 회피 — unmappable 과 구분: git 오류는 skip, 형식상 매핑불가는 unmappable). `verified_at`
    = pin(호출부가 anchor 유효성 검증 후 전달·range 양끝). `git_runner` 미지정 시 `_real_git_runner(repo)`.
    """
    repo = repo or REPO
    runner = git_runner or _real_git_runner(repo)
    empty_tree = _empty_tree_oid(runner)   # object-format 중립 빈 트리 OID·1회 산출
    present: list[str] = []
    absent: list[str] = []
    unmappable: list[str] = []
    for glob in covers:
        if not str(glob).strip():
            continue  # 빈/공백 = 패턴 아님 → 제외(코드-무관·unmappable 아님).
        spec = covers_glob_pathspec(glob)
        if spec is None:
            unmappable.append(glob)  # 지원 안 하는 형태(비-경계 `**`·repo 밖/절대 경로 등) — advisory.
            continue
        observable = _pathspec_observable(runner, spec, verified_at, empty_tree)
        if observable is None:
            continue  # git 오류·빈 트리 OID 산출 실패 — 거짓 분류 없이 skip(fail-soft).
        (present if observable else absent).append(glob)  # 원본 글롭 보고
    return present, absent, unmappable


def _parse_updated_date(updated) -> datetime.date | None:
    """페이지 `updated` 를 date 로 정규화한다 (board.load_ticket=yaml.safe_load).

    YAML 의 따옴표 없는 `2026-06-19` 는 `datetime.date` 로, 따옴표/기타는 문자열로 온다.
    date/datetime 는 그대로 date 화, 문자열은 ISO 앞 10자(`YYYY-MM-DD`)를 파싱한다. 파싱
    실패/None/기타 타입 → None(호출부 page_stale 이 unknown 으로 흡수·fail-soft).
    """
    if isinstance(updated, datetime.datetime):
        return updated.date()
    if isinstance(updated, datetime.date):
        return updated
    if isinstance(updated, str):
        try:
            return datetime.date.fromisoformat(updated.strip()[:10])
        except ValueError:
            return None
    return None


def _parse_commit_date(out: str) -> datetime.date | None:
    """`git log --format=%cI` ISO 출력에서 커밋 날짜(date 부분)를 뽑는다.

    `%cI` = strict ISO(`2026-06-19T07:59:00+09:00`). 앞 10자(`YYYY-MM-DD`)만 date 비교에
    쓴다(시각/타임존 무시 — updated 가 날짜 단위라 date 끼리 비교). 빈/깨진 출력 → None.
    """
    out = out.strip()
    if not out:
        return None
    try:
        return datetime.date.fromisoformat(out[:10])
    except ValueError:
        return None


def page_stale(page: dict, *,
               git_runner: GitRunner | object | None = None) -> bool | None:
    """페이지 covers 코드가 페이지 `updated` *후* git 커밋된 적 있으면 stale.

    `True` = stale(최신 covers 커밋 날짜 > updated)·`False` = fresh(커밋이 updated 이하)·
    **`None` = 판정불가(fail-soft·unknown)**. None 이 되는 경우:
      - covers 가 비었다(코드-무관 개념 — 평가 대상 없음).
      - `updated` 파싱 실패(부재·깨짐).
      - covers 글롭이 전부 pathspec 매핑 불가(빈/공백 또는 미지원 문법 — 비-경계 `**`·절대
        경로 등·`covers_glob_pathspec` 이 None) — 좁힐 대상 없음.
      - git 호출 실패(rc≠0·git 부재/에러) 또는 빈 출력(미추적·커밋 0).
    crash 0 — git 없는 환경(솔로/CI)도 무탈히 unknown.

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

    updated_date = _parse_updated_date(page.get("updated"))
    if updated_date is None:
        return None

    pathspecs = [ps for ps in (covers_glob_pathspec(g) for g in covers) if ps]
    if not pathspecs:
        return None

    if git_runner is _UNRESOLVED_GIT_RUNNER:
        return None
    runner = git_runner or _page_owner_git_runner(page)
    if runner is None:
        return None
    try:
        rc, out = runner(["log", "-1", _GIT_LOG_FORMAT, "--", *pathspecs])
    except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 unknown(None).
        return None
    if rc != 0:
        return None

    commit_date = _parse_commit_date(out)
    if commit_date is None:
        return None
    return commit_date > updated_date


# ── 페이지 파싱 · 스캔 ────────────────────────────────────────────────────────


class DomainPageEnumerationError(RuntimeError):
    """strict 페이지 열거 중 특정 문서를 읽거나 파싱하지 못한 오류."""

    def __init__(self, path: Path, cause: Exception):
        self.path = Path(path)
        super().__init__(f"{self.path}: {cause}")


def parse_page(path: Path) -> dict:
    """한 domain 페이지를 파싱한다.

    Returns: {path, title, type, covers: list[str], derived: bool, updated, status,
    verified_at, repo}.
    frontmatter 파싱은 board.load_ticket 재사용(임의 frontmatter md 파서·DRY).
    covers 부재 → []·derived 부재 → False·status 부재 → None(정식 취급·draft 아님).
    `verified_at` 은 이 페이지 지식이 대조한 검증 기준 커밋 sha(board.py freshness
    lint 가 "그 sha 이후 covers 경로 커밋 있나"로 판정) — 부재/비-문자열 → None(freshness skip).
    `repo` 는 그 sha/covers 를 판정할 **소유 저장소 시계**(`self` | `upstream`) — 키 부재만
    `self` 로 자연 퇴화한다. 명시 빈 문자열/false/0/컨테이너는 그대로 보존해 board.py가
    `domain-unverifiable`로 표면화한다.
    board 미로드/frontmatter 깨짐은 호출부가 처리하도록 예외를 그대로 전파한다(load_pages
    가 graceful skip). `status` 는 capture-draft 가 쓴 `draft` 진실 — load_pages 가 이로
    미승인 초안을 index 에서 제외한다(suffix 가 아닌 frontmatter status 가 필터 기준).
    """
    board = _load_board()
    if board is None:
        raise RuntimeError(f"board.py 로드 실패 ({TOOLS_DIR / 'board.py'} 부재 또는 로드 오류).")
    fm, _body = board.load_ticket(path)
    # covers 정규화 — 스칼라 문자열은 단일 글롭으로 감싼다(`list("src/x/**")` 가
    # 글자 분해돼 분해된 '*' 가 임의 단일-세그먼트 경로를 거짓 매칭하는 것 방지).
    # list 면 문자열 원소만, None·기타 타입은 빈 리스트(코드-무관).
    covers = fm.get("covers")
    if isinstance(covers, str):
        covers = [covers]
    elif isinstance(covers, list):
        covers = [c for c in covers if isinstance(c, str)]
    else:
        covers = []
    # status — 문자열만 취한다(부재·비-문자열 → None = 정식 페이지·draft 제외 대상 아님).
    status = fm.get("status")
    status = status if isinstance(status, str) else None
    # verified_at — 검증 기준 sha(부재/빈값 → None = freshness lint skip). YAML 이 짧은
    # all-digit sha 를 int 로 파싱해도 board 쪽 str() 처리와 대칭이게 str 정규화(codex suggestion).
    verified_at = fm.get("verified_at")
    verified_at = str(verified_at).strip() or None if verified_at is not None else None
    if "repo" not in fm:
        owner_repo = "self"
    else:
        owner_repo = fm.get("repo")
        if isinstance(owner_repo, str):
            owner_repo = owner_repo.strip()  # 명시 `repo: ""`는 "" 유지(self 흡수 금지).
    return {
        "path": path,
        "title": fm.get("title") or "",
        "type": fm.get("type") or "",
        "covers": covers,
        "derived": bool(fm.get("derived")),
        "updated": fm.get("updated"),
        "status": status,
        "verified_at": verified_at,
        "repo": owner_repo,
    }


def load_pages(domain_dir: Path = DOMAIN_DIR, *, strict: bool = False) -> list[dict]:
    """domain/ 의 `*.md` 를 **재귀**(rglob) 스캔해 파싱된 페이지 리스트를 돌려준다.

    domain wikitree 를 하위 폴더로 조직해도 그 안의 페이지가 잡히도록 `rglob` 로 재귀
    스캔한다(회사 실사용). README.md·_template.md 는 (어느 깊이든) `name` 으로 제외.
    디렉토리 부재 → [](solo·신규 clone 무영향). 평면 domain/ 은 하위폴더가 없어 결과 불변(additive).

    **frontmatter-less 조용한 skip**: `---` 구분자로 시작하지 않는 `.md`(tmp·메모 등
    다수)는 "페이지 아님" — 개별 경고 없이 조용히 skip 하고 디렉토리별 카운터에만 누적한다.
    스캔 종료 시 skip 이 1개 이상이면 stderr 에 디렉토리별 개수 요약 딱 1줄만 남긴다(파일
    전체 목록 X — LLM 컨텍스트 낭비 원인). 반면 `---` 로 시작하는데 parse 가 깨지는(malformed)
    파일은 진짜 페이지 오류 신호라 기존 개별 경고를 유지한다(crash 0).

    `strict=True`는 쓰기 명령의 validate-all-first 대상 열거용이다. frontmatter로 시작한
    문서의 읽기/파싱 실패를 `DomainPageEnumerationError(path, cause)`로 즉시 전파한다.
    기본값 False는 조회/lint의 기존 graceful skip·경고 동작을 그대로 보존한다.

    **draft 제외**: frontmatter `status == "draft"` 페이지는 미승인 초안
    (capture-draft scaffold)이라 index 에서 뺀다 — affected/lint/recall/capture 가
    승인 안 된 지식을 보지 않게. 필터 기준은 frontmatter status 이지 `.draft.md` 파일명이
    아니다(promote = status:draft 제거 1개로 비로소 정식 = 포함). status 부재/기타 → 포함.
    """
    domain_dir = Path(domain_dir)
    if not domain_dir.is_dir():
        return []
    pages: list[dict] = []
    non_page_counts: dict[str, int] = {}  # frontmatter-less skip 카운트(디렉토리별).
    for path in sorted(domain_dir.rglob("*.md")):
        if path.name in _NON_PAGE_FILES:
            continue
        # frontmatter 구분자(`---` 시작)가 아예 없는 파일 = 페이지 아님(tmp·메모 등). 개별
        # 경고 없이 조용히 skip 하고 디렉토리별 카운터에만 누적(스캔 종료 시 요약 1줄).
        # 읽기 실패는 판정 불가 → parse_page 로 넘겨 malformed 경고에 맡긴다. UnicodeDecodeError
        # (non-UTF-8 tmp — cp949·바이너리)는 OSError 가 아니라 별도 포획 — 안 잡으면 load_pages
        # 전체가 크래시해 crash-0 보장이 깨진다.
        try:
            has_delimiter = path.read_text(encoding="utf-8").lstrip().startswith("---")
        except (OSError, UnicodeDecodeError) as exc:
            if strict:
                raise DomainPageEnumerationError(path, exc) from exc
            has_delimiter = True
        if not has_delimiter:
            key = path.parent.relative_to(domain_dir).as_posix()  # flat → "."
            non_page_counts[key] = non_page_counts.get(key, 0) + 1
            continue
        try:
            page = parse_page(path)
        except Exception as exc:  # noqa: BLE001 — 기본 조회는 깨진 페이지 skip·strict는 전파.
            if strict:
                raise DomainPageEnumerationError(path, exc) from exc
            print(f"domain: {path.name} 파싱 skip — {exc}", file=sys.stderr)
            continue
        if page["status"] == DRAFT_STATUS:
            continue  # 미승인 초안 — index 제외(promote 전까지 안 보임).
        pages.append(page)
    if non_page_counts:
        total = sum(non_page_counts.values())
        breakdown = ", ".join(f"{d}: {n}" for d, n in sorted(non_page_counts.items()))
        print(f"domain: frontmatter 없는 파일 {total}개 skip ({breakdown})", file=sys.stderr)
    return pages


@_repository_query_batch()
def pages_for_path(
        path: str, pages: list[dict], *, warn_owner_mismatch: bool = True) -> list[dict]:
    """주어진 repo-relative 코드 경로를 covers 글롭으로 담는 페이지들을 돌려준다.

    경로 구분자를 POSIX(`/`)로 정규화해 매칭한다(Windows 백슬래시 무관). os.sep 뿐
    아니라 백슬래시도 직접 치환해 POSIX 실행 중 들어온 Windows 경로도 정규화한다. 빈
    covers 페이지(코드-무관 개념)는 어떤 경로도 매치하지 않는다. normalized touch는
    owner 채널뿐 아니라 검증된 workspace의 **저장소 정체성**도 소비한다. 페이지 ``repo:``
    채널을 ``board._freshness_owner_repo``로 해소한 checkout과 workspace가 같은 git
    common-dir를 공유할 때만 매치한다. 소유 checkout/정체성 미해소·별개 저장소와 self 채널
    탈락은 advisory를 낸다.
    """
    owner = getattr(path, "owner", None)
    touch_repo = getattr(path, "repo", None)
    touch_workspace = getattr(path, "workspace", None)
    norm = path.replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    covers_matches = [
        page for page in pages
        if _path_or_directory_matches_covers(norm, page["covers"])
    ]
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
        page_repo, owner_error = _page_owner_repo(page)
        if page_repo is None:
            unresolved.append(owner_error or "페이지 repo 소유 checkout 미해소")
            continue
        try:
            workspace = Path(touch_workspace).resolve()
        except (OSError, RuntimeError) as exc:
            unresolved.append(f"touch workspace 실경로 해소 실패: {exc}")
            continue
        same_repository, identity_error = _same_repository_checkouts(
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
    except Exception:  # noqa: BLE001 — 알 수 없는 str subclass도 좌표를 잃지 않고 fail-soft.
        return _CoordinatePath(
            relative,
            owner=owner,
            repo=repo,
            workspace=workspace,
        )


def _directory_touch_location(touch: str) -> tuple[Path, Path, str] | None:
    """실재하며 checkout 안에 있는 디렉토리 touch의 (checkout, directory, norm)."""
    workspace = getattr(touch, "workspace", None)
    checkout = Path(workspace) if workspace is not None else REPO
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


def _files_for_directory_touch(touch: str) -> list[str]:
    """실재 디렉토리 touch를 그 checkout 아래 파일 좌표들로 전개한다.

    git checkout이면 tracked + untracked(non-ignored)를 사용해 ``__pycache__`` 같은 ignore
    산출물이 gap에 섞이지 않게 한다. git이 반환한 파일형 엔트리는 dangling/directory symlink와
    gitlink도 포함해 그대로 신뢰한다. git을 쓸 수 없는 solo 디렉토리만 실제 일반 파일을 찾는
    파일시스템 순회로 fallback한다. 파일/미해소/checkout 밖 경로는 원 touch 하나를 그대로
    보존한다. 실재하지만 비었거나 모든 파일이 gitignored인 디렉토리는 빈 목록으로 전개돼
    gap에서 사라진다.
    """
    location = _directory_touch_location(touch)
    if location is None:
        return [touch]
    checkout, directory, norm = location

    repo_files = _load_repo_owned_files()
    try:
        relative_files = repo_files.list_repo_owned_files(
            checkout,
            norm,
            mode=repo_files.OWNED,
            git_runner=_real_git_runner(checkout),
        )
    except OSError:
        return [touch]

    expanded: list[str] = []
    reconstruction_failures = 0
    for relative_path in relative_files:
        relative = relative_path.as_posix()
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
        touches: list[str] | None,
        pages: list[dict] | None = None,
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
    if pages is None:
        pages = load_pages(DOMAIN_DIR)
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
        for candidate in _files_for_directory_touch(norm):
            key = (
                getattr(candidate, "owner", None),
                getattr(candidate, "repo", None),
                str(candidate),
            )
            if key in seen:
                continue
            seen.add(key)
            if not pages_for_path(
                    candidate, pages, warn_owner_mismatch=warn_owner_mismatch):
                group.append(candidate)
        groups.append((norm, group))
    return groups


@_repository_query_batch()
def uncovered_paths(
        touches: list[str] | None,
        pages: list[dict] | None = None,
        *,
        warn_owner_mismatch: bool = True,
) -> list[str]:
    """touch 경로 중 *어느 페이지 covers 글롭에도 안 잡힌* 파일 전체를 돌려준다.

    capture(채록)의 gap 검출 — touched 코드인데 담당 domain 페이지가 없는 경로 = 후보
    신규 페이지. 실재 디렉토리 touch는 checkout의 파일 단위로 전개한 뒤 각 파일에
    `pages_for_path`를 적용해 매칭 0 인 것만 남긴다. 이 전개는 gap 검출 전용이며
    `pages_for_path`/`pages_for_touches`의 디렉토리 소환 recall은 바꾸지 않는다.
    **발견 순서 보존·dedup 키=(owner, repo, 경로)**라 같은 repo 좌표 중복만 접고, 문자열
    값이 같은 self/upstream·서로 다른 repo gap은 각각 보존한다. 비-문자열 touch·빈/공백
    경로는 방어적으로 건너뛴다(`pages_for_touches` 동형). 빈/None touches → [].

    반환값은 출력 상한과 무관한 **전체 목록**이다. 기본 ``capture`` 출력만 디렉토리 touch별
    상한을 적용하며 ``capture --all-gaps``는 이 전체 목록과 같은 정보를 표시한다.

    `pages` 미주입 시 `load_pages()`(실 domain/ 스캔·부재 시 []). domain/ 가 비면 *모든*
    touch 가 uncovered (담당 페이지 0) — solo·신규 clone 무영향(capture 가 gap 절을 띄움).
    """
    groups = _uncovered_path_groups(
        touches,
        pages,
        warn_owner_mismatch=warn_owner_mismatch,
    )
    return [path for _touch, paths in groups for path in paths]


@_repository_query_batch()
def pages_for_touches(touches: list[str] | None, pages: list[dict] | None = None) -> list[dict]:
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
    if pages is None:
        # 모듈 전역 DOMAIN_DIR 을 호출 시점에 읽는다 — load_pages 인자 기본값은 정의
        # 시점에 굳어 monkeypatch(테스트)·재바인딩을 못 본다(cmd_list 동형).
        pages = load_pages(DOMAIN_DIR)
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
        for page in pages_for_path(norm, pages):
            key = page["path"]
            if key in seen:
                continue
            seen.add(key)
            out.append(page)
    return out


# ── freshness lint (advisory·exit 0·비차단) ──────────────────────
# 페이지를 스캔해 advisory finding 을 낸다(stale/orphan/oversized). **막지 않는다** —
# 전부 exit 0(visibility). unknown(stale==None)은 finding 아님.

# domain 페이지 본문의 wikilink `[[슬러그]]`(별칭 `[[슬러그|텍스트]]` 의 슬러그 부분만).
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")


def page_slug(page: dict) -> str:
    """페이지 슬러그 = 파일 stem(`dual-gate-review.md` → `dual-gate-review`).

    domain wikilink 표기 관례(`[[ADR-0018]]`·`[[T-0080]]`·README 의 `[[다른-페이지]]`)는
    파일 stem 을 슬러그로 쓴다. orphan 인링크 카운트의 정규 키 — title 변형이 아닌 안정
    식별자(파일명)로 잡는다(아래 inlink 집합도 stem·title 둘 다 인정해 표기 흔들림 흡수).
    """
    return Path(page["path"]).stem


def _page_body(page: dict) -> str:
    """페이지 body(frontmatter 뒤 본문)를 읽는다 — board.load_ticket 재사용(DRY).

    parse_page 는 frontmatter 만 담으므로 lint(인링크/라인수)용 body 를 따로 읽는다. 읽기/
    파싱 실패는 빈 문자열(fail-soft — 깨진 페이지가 lint 를 죽이지 않게·load_pages 가 이미
    파싱 가능한 것만 넘겼지만 방어적으로 흡수).
    """
    board = _load_board()
    if board is None:
        return ""
    try:
        _fm, body = board.load_ticket(page["path"])
    except Exception:  # noqa: BLE001 — fail-soft: 읽기 실패는 빈 body(lint 무crash).
        return ""
    return body


def _wikilink_targets(body: str) -> set[str]:
    """body 의 `[[슬러그]]` 인링크 타깃 집합(소문자 정규화·별칭 `|` 앞부분만)."""
    return {m.strip().lower() for m in _WIKILINK_RE.findall(body)}


def lint_pages(pages: list[dict], *, git_runner: GitRunner | None = None,
               git_runner_factory: GitRunnerFactory | None = None,
               oversized_lines: int = OVERSIZED_LINES) -> list[tuple[str, str, str]]:
    """domain 페이지 스캔 → finding 리스트 `(kind, page, detail)` (advisory·비차단).

    kind ∈ {`stale`, `orphan`, `oversized`}:
      - **stale** — `page_stale==True`(covers 코드가 updated 후 커밋). unknown(None)은 제외.
      - **orphan** — 다른 domain 페이지에서 이 페이지로의 `[[슬러그]]` 인링크 0(고립). 슬러그
        (파일 stem)와 title 둘 다 인링크로 인정(표기 흔들림 흡수). **자기참조 제외**(자기 body
        의 자기링크는 안 침)·README/_template 은 애초에 load_pages 가 뺀다. **페이지 ≥2 일 때만
        평가** — 1개뿐이면 peer 가 없어 자연 고립이라 skip.
      - **oversized** — body 라인수 > `oversized_lines`(기본 OVERSIZED_LINES=200).

    finding 은 page 표시명(title 우선·없으면 슬러그)으로 라벨한다. clean(빈 리스트)이면
    호출부가 "✓ domain freshness 양호" 를 찍는다. git 은 page_stale 의 DI seam 으로 위임.
    `git_runner_factory`가 있으면 페이지마다 소유 repo runner를 고르고, 없으면 기존 단일
    `git_runner`(테스트/구 호출 호환) 또는 page_stale의 owner 해소를 쓴다.
    """
    findings: list[tuple[str, str, str]] = []

    # 모든 페이지 body 를 한 번 읽어 (a) 전역 인링크 집합·(b) 페이지별 라인수를 모은다.
    bodies = {page_slug(p): _page_body(p) for p in pages}

    # 전역 인링크 집합 — 자기 body 의 자기링크는 제외(self-ref 가 orphan 을 가리지 않게).
    # 한 페이지가 자기를 **슬러그로든 title 로든** 가리킨 링크는 모두 뺀다 — orphan 판정이
    # slug·title 둘 다 인링크로 인정하므로(아래), 자기참조 제외도 slug·title 둘 다여야
    # false-negative 가 없다(`[[자기-title]]` 자기참조가 고립 페이지를 살려내는 갭 차단).
    inlinks: set[str] = set()
    for page in pages:
        slug = page_slug(page)
        self_keys = {slug.lower()}
        title_key = (page["title"] or "").strip().lower()
        if title_key:
            self_keys.add(title_key)
        for target in _wikilink_targets(bodies[slug]):
            if target in self_keys:
                continue  # 자기참조(슬러그·title)는 인링크로 안 침.
            inlinks.add(target)

    for page in pages:
        slug = page_slug(page)
        label = page["title"] or slug

        # stale — page_stale==True 만(unknown=None 은 finding 아님).
        page_runner = (git_runner_factory(page) or _UNRESOLVED_GIT_RUNNER
                       if git_runner_factory is not None else git_runner)
        if page_stale(page, git_runner=page_runner) is True:
            findings.append(("stale", label, f"covers 코드가 updated({page['updated']}) 후 커밋됨"))

        # orphan — 슬러그/title 어느 표기로도 인링크 0.
        # 단 페이지가 1개뿐이면 orphan 판정 skip — 인링크할 *peer 가 존재하지 않아* 자연
        # 고립이고(첫 페이지는 항상 orphan), 매 lint 마다 의미 없는 advisory 가 떠 "clean"
        # 시그널을 흐린다. orphan 은 peer(≥2 페이지)가 있을 때만 의미 있다.
        title_key = (page["title"] or "").strip().lower()
        keys = {slug.lower()}
        if title_key:
            keys.add(title_key)
        if len(pages) >= 2 and keys.isdisjoint(inlinks):
            findings.append(("orphan", label, "다른 domain 페이지에서 인링크 0 (고립)"))

        # oversized — body 라인수 임계 초과.
        line_count = len(bodies[slug].splitlines())
        if line_count > oversized_lines:
            findings.append(("oversized", label, f"본문 {line_count}줄 > {oversized_lines}"))

    return findings


# ── CLI ───────────────────────────────────────────────────────────────────────


def _stale_marker(page: dict, *, git_runner: GitRunner | None = None) -> str:
    """페이지 줄 앞 stale 마커 — stale(True)=`⚠ `·None(unknown)/False=무표시(공백 정렬).

    list/affected 가 공유한다(DRY). page_stale==True 만 ⚠ — unknown(git 부재 등)은 조용히
    무표시(노이즈 방지). 마커 폭(2칸)을 비-stale 줄에도 채워 줄을 정렬한다.
    """
    return "⚠ " if page_stale(page, git_runner=git_runner) is True else "  "


def cmd_list(args: argparse.Namespace) -> int:
    # 모듈 전역 DOMAIN_DIR 을 명시 전달 — 인자 기본값은 정의 시점에 굳어 monkeypatch(테스트)·
    # 재바인딩을 못 본다. cmd_list 는 호출 시점의 전역을 읽게 한다.
    pages = load_pages(DOMAIN_DIR)
    if not pages:
        print("(domain 페이지 없음)")
        return 0
    for page in pages:
        marker = _stale_marker(page)
        title = page["title"]
        ptype = page["type"]
        covers = ",".join(page["covers"])
        updated = page["updated"] or ""
        print(f"{marker}{title}  ·  {ptype}  ·  {covers}  ·  {updated}")
    return 0


def _touches_from_ticket(ticket_id: str) -> list[str]:
    """board.load_ticket 으로 ticket frontmatter 의 `touches` 를 읽는다(없으면 []).

    board 미로드·ticket 부재/깨짐 → [](graceful·crash 0). frontmatter `touches` 의
    문자열 원소만 취한다(비-문자열 오기는 방어적으로 버림 — parse_page covers 동형).
    """
    board = _load_board()
    if board is None:
        print("domain: board.py 로드 실패 — touches 를 읽지 못했다.", file=sys.stderr)
        return []
    try:
        _status, path = board.find_ticket(ticket_id)
        fm, _body = board.load_ticket(path)
    except Exception as exc:  # noqa: BLE001 — 부재/깨진 ticket 은 graceful(빈 touches).
        print(f"domain: ticket {ticket_id} touches 읽기 skip — {exc}", file=sys.stderr)
        return []
    touches = fm.get("touches")
    if isinstance(touches, str):
        return [touches.strip()] if touches.strip() else []
    if isinstance(touches, list):
        # --touches CLI 와 동형: 각 원소 strip·빈 값/비-문자열 drop (silent-miss 방어).
        return [t.strip() for t in touches if isinstance(t, str) and t.strip()]
    return []


def cmd_affected(args: argparse.Namespace) -> int:
    """ticket touches(또는 --touches) ∩ domain covers 로 영향받는 페이지를 출력한다."""
    if args.ticket:
        touches = _touches_from_ticket(args.ticket)
        try:
            # --ticket touches만 PM-home 좌표 계약을 가진다. 사용자가 직접 넘긴 --touches는
            # 이미 이 CLI의 repo 상대 입력 계약이므로 변환하지 않는다.
            touches = _normalize_ticket_touches(touches)
        except RuntimeError as exc:
            print(
                f"domain: ticket {args.ticket} touches 좌표 정규화 실패 — {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        # --touches a,b,c — 콤마분리·공백 trim·빈 토큰 제거.
        touches = [t.strip() for t in args.touches.split(",") if t.strip()]
    pages = pages_for_touches(touches, load_pages(DOMAIN_DIR))
    if not pages:
        print("(영향 domain 페이지 없음)")
        return 0
    for page in pages:
        marker = _stale_marker(page)
        title = page["title"]
        covers = ",".join(page["covers"])
        print(f"{marker}{title}  ·  {covers}")
    return 0


def _touches_from_tickets(tickets_csv: str) -> list[str]:
    """콤마분리 ticket ID 목록의 touches 를 집계한다 (각각 `_touches_from_ticket`·DRY).

    `--tickets T-a,T-b` → 각 ticket frontmatter touches 의 union(발견 순서 보존·dedup).
    공백 trim·빈 토큰 제거(`--touches` CLI 동형). 부재/깨진 ticket 은 `_touches_from_ticket`
    이 graceful 하게 [] 를 돌려주므로 crash 0(그 ticket 만 조용히 빈 기여).
    """
    seen: set[str] = set()
    out: list[str] = []
    for tid in (t.strip() for t in tickets_csv.split(",")):
        if not tid:
            continue
        for touch in _touches_from_ticket(tid):
            if touch in seen:
                continue
            seen.add(touch)
            out.append(touch)
    return out


@_repository_query_batch()
def cmd_capture(args: argparse.Namespace) -> int:
    """세션이 건드린 코드의 담당 domain 페이지를 "갱신 검토" 대상으로 띄운다.

    recall의 쓰기 측 짝 — *무엇을 갱신/신설할지* 띄울 뿐 본문을 자동 생성/스탬프하지
    않는다(surface-only·자동 `updated:` 금지 → stale 탐지 거짓 방지·결정 절). 두 절 출력:
      1. **영향 페이지** — touches ∩ covers 매칭(`pages_for_touches`) + `⚠ ` stale 마커.
      2. **coverage gap** — 어느 페이지 covers 에도 안 잡힌 touch 경로(`uncovered_paths`)
         = 후보 신규 페이지. 비면 절 생략.
    둘 다 없으면 `(채록할 domain 변화 없음)`. 정상 좌표에서는 **read-only·exit 0**
    (advisory·작업 무차단). ticket의 worktree 접두가 lease/경로 안전성 검증에 실패하면 해당
    touch를 stderr 경고 후 제외하고 나머지를 계속 조회한다(exit 0). 쓰기 표면의 stage는 같은
    좌표 오류를 빈 스코프로 흘리지 않고 명시 오류로 차단한다(fail-loud).
    """
    if args.tickets:
        # --tickets 는 nargs='+' — 공백 나열(T-a T-b)·콤마분리("T-a,T-b") 둘 다 수용한다
        # (CLI 관용·"mechanize don't instruct"·자연스러운 나열이 usage 에러로 실패하던 클래스
        # 소멸). 토큰 리스트를 콤마로 합쳐 기존 콤마-split 집계(_touches_from_tickets)에
        # 위임 — 콤마-단일-문자열 호환을 그대로 보존한다(join 후 split 이 양형식을 흡수). ticket
        # ID 는 공백을 안 담으므로 space-split 이 무모호(경로를 받는 --touches 는 콤마 유지·비대칭).
        touches = _touches_from_tickets(",".join(args.tickets))
        try:
            touches = _normalize_ticket_touches(touches)
        except RuntimeError as exc:
            print(
                f"domain: tickets touches 좌표 정규화 실패 — {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        # --touches a,b,c — affected 동형(콤마분리·공백 trim·빈 토큰 제거).
        touches = [t.strip() for t in args.touches.split(",") if t.strip()]

    pages = load_pages(DOMAIN_DIR)
    affected = pages_for_touches(touches, pages)
    # pages_for_touches가 owner-mismatch advisory를 이미 냈다. gap 계산은 같은 touch를 다시
    # 보므로 여기서는 중복 경고만 끄고 owner 필터/coverage 판정은 그대로 쓴다.
    gap_groups = _uncovered_path_groups(
        touches, pages, warn_owner_mismatch=False)
    gaps = [
        path
        for _touch, group_paths in gap_groups
        for path in group_paths
    ]

    if not affected and not gaps:
        judgment_targets: list[str] = []
        expanded_file_count = 0
        expanded_directory_touch = False
        for touch in touches:
            if not isinstance(touch, str) or not touch.strip():
                continue
            norm = touch if touch.strip() == touch else touch.strip()
            is_directory_touch = _directory_touch_location(norm) is not None
            expanded = _files_for_directory_touch(norm)
            judgment_targets.extend(expanded)
            if is_directory_touch:
                expanded_directory_touch = True
                expanded_file_count += len(expanded)
        if touches and judgment_targets:
            expansion = (
                f", 전개된 파일 {expanded_file_count}개"
                if expanded_directory_touch
                else ""
            )
            print(
                f"domain: capture 판정 불일치 의심 — touch {len(touches)}개"
                f"{expansion}, 판정 대상 {len(judgment_targets)}개인데 "
                "affected·coverage gap 모두 0개"
            )
            return 0
        print("(채록할 domain 변화 없음)")
        return 0

    if affected:
        print("영향 페이지 (갱신 검토):")
        for page in affected:
            marker = _stale_marker(page)
            covers = ",".join(page["covers"])
            print(f"  {marker}{page['title']}  ·  {covers}")
    if gaps:
        print("coverage gap (후보 신규 페이지 — 담당 covers 없음):")
        for touch, group_paths in gap_groups:
            visible = group_paths
            if (
                    not getattr(args, "all_gaps", False)
                    and len(group_paths) > DIRECTORY_GAP_DISPLAY_LIMIT):
                # 비-디렉토리 touch는 전개 결과가 최대 한 경로라 이 길이 조건에 못 들어온다.
                visible = group_paths[:DIRECTORY_GAP_DISPLAY_LIMIT]
            for path in visible:
                print(f"  {path}")
            hidden = len(group_paths) - len(visible)
            if hidden:
                print(
                    f"  … 외 {hidden}개 (총 {len(group_paths)}개)"
                    f" — 디렉토리 {touch}"
                )
    return 0


# ── capture-draft (researcher 조사 prose → domain 초안 scaffold·git 무조작) ──
# researcher 의 *조사 prose*(read-only gather 산출)를 domain draft 페이지로 scaffold 한다.
# **기계는 scaffold + verbatim prose 배치까지만** — type/covers 정련·의미 판단은 PM/LLM·
# promote(draft→정식)는 사람 손. no-auto-commit 3중: (1) frontmatter `status: draft` 가
# index 제외 진실 (2) git add/commit 절대 호출 안 함(파일만 쓰고 staging 무변화) (3) promote
# 명령 부재 = draft→정식 전환이 사람 손(status 제거 + .draft.md→.md rename + git add).

# kebab 슬러그 변환 — 영숫자만 남기고 그 외(공백·기호·한글)는 하이픈, 중복/양끝 하이픈 정리.
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """title 을 kebab-case 슬러그로 변환한다 (--slug 미지정 시 파일명 도출).

    소문자화 후 영숫자(ascii) 외 문자열을 단일 하이픈으로 접고 양끝 하이픈을 제거한다.
    한글 등 비-ascii 는 영숫자가 아니라 하이픈으로 접히므로, 영숫자가 전혀 없는 title
    (순한글 등)은 빈 슬러그가 된다 → 호출부(cmd)가 `draft` 기본 슬러그로 대체한다.
    """
    slug = _SLUG_NON_ALNUM_RE.sub("-", title.lower()).strip("-")
    return slug


def _read_source(source: str | None) -> str:
    """`--source` 입력(조사 prose)을 읽는다 — `-`=stdin·파일경로=파일·None/(none)=빈 문자열.

    프로비넌스 표기 `(none)`(미지정)도 빈 본문으로 흡수한다. 파일 부재/읽기 실패는 호출부가
    명시 에러로 보고하도록 예외를 전파한다(scaffold 전에 잡혀 잘못된 빈 페이지 생성 방지).
    """
    if source is None or source == "(none)":
        return ""
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


# 코드펜스 토글 — 라인-시작 ``` 또는 ~~~ (3+ 백틱/틸드)이 펜스 경계(CommonMark 는 둘 다 펜스).
# 펜스 안의 `## ` 는 마크다운 헤딩이 아니라 코드/주석(예 shell `## comment`)이므로 강등에서 제외
# (주석/마크다운 펜스 보호). group(1)=펜스 문자 — CommonMark 정합상 여는 펜스와 닫는 펜스 문자가
# 같아야 닫힌다(``` 로 열면 ``` 로 닫힘·~~~ 로 열면 ~~~ 로 닫힘) → mixed 펜스 오토글 방지.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
# 라인-시작 `## ` (정확히 2개 — `###`+ 는 더 깊어 scaffold 절과 충돌 안 함). `##` 만 한 단계
# 강등(dogfood 사례 충분·단순). `###`+ 전체 깊이 시프트는 over-engineering 이라 생략.
_PROSE_H2_RE = re.compile(r"^## (?!#)")


def _demote_prose_headings(prose: str) -> str:
    """prose 본문의 라인-시작 `## ` 헤딩을 `### ` 로 한 단계 강등한다(scaffold 절 충돌 방지).

    capture-draft 는 prose 를 `## 조사 결과` 절 *아래* verbatim 배치하는데, prose 가 자체
    `## ` 헤딩을 가지면 그게 페이지 절(`## 한 줄`·`## gotcha`)과 같은 레벨 형제로 떠 구조가
    어긋난다. `## `(정확히 H2)만 `### ` 로 강등해 scaffold 절 하위로 일관 배치.

    코드펜스(```·~~~) 안의 `## ` 는 마크다운 헤딩이 아니라 코드/주석이므로 제외한다 — 펜스
    토글을 추적해 펜스 밖 라인만 강등. CommonMark 정합상 닫는 펜스는 *여는 펜스와 같은 문자*
    여야 닫히므로(`~~~` 안의 ``` 는 펜스를 닫지 않음), 여는 펜스 문자(`fence_char`)를 기억해
    동일 문자에서만 닫는다(mixed 펜스 오토글 방지). `###`+ 는 이미 더 깊어 scaffold 절과 충돌
    하지 않으므로 손대지 않는다(상대 깊이 시프트는 dogfood 불요).
    """
    fence_char = ""  # "" = 펜스 밖 · "`"/"~" = 그 문자로 연 펜스 안.
    out_lines = []
    for line in prose.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            char = fence.group(1)[0]  # 펜스 문자(백틱/틸드).
            if not fence_char:
                fence_char = char       # 펜스 진입(여는 문자 기억).
            elif char == fence_char:
                fence_char = ""         # 같은 문자에서만 펜스 종료.
            # (펜스 안에서 다른 문자 펜스 라인 → 코드 내용·토글 안 함.)
            out_lines.append(line)
            continue
        if not fence_char and _PROSE_H2_RE.match(line):
            line = "#" + line  # `## ` → `### ` (한 단계 강등).
        out_lines.append(line)
    return "\n".join(out_lines)


def _normalize_source_label(source: str | None) -> str:
    """`--source` 입력을 frontmatter `source:` 에 박을 provenance 라벨로 정규화한다.

    절대경로/일시경로 박제를 막는다(promote 후 dangling). 규칙:
      - stdin(`-`)·미지정(None) → placeholder(자유서술·PM 손).
      - repo 내 파일경로 → **repo 상대경로**(절대경로 아님).
      - repo 밖 경로(일시 `/tmp/...` 포함) → placeholder(절대경로 박제 금지).

    repo 루트 판정은 모듈 REPO 상수(스크립트-위치 앵커) 재사용. repo-내/밖 이분으로 단순화 —
    repo 밖이면 이미 placeholder 라 tmp 별도 패턴 판정 불요(`/tmp/...` 는 repo 밖이므로 흡수됨).
    """
    if source is None or source == "(none)" or source == "-":
        return SOURCE_TODO_PLACEHOLDER
    try:
        resolved = Path(source).resolve()
        relative = resolved.relative_to(REPO)
    except (ValueError, OSError):
        # ValueError = repo 밖(relative_to 실패) · OSError = resolve 불가 → placeholder.
        return SOURCE_TODO_PLACEHOLDER
    return relative.as_posix()


def _draft_frontmatter(title: str, ptype: str, covers: list[str],
                       source: str, today: str) -> str:
    """draft 페이지 frontmatter(scaffold) 문자열을 만든다.

    `status: draft`(index 제외 진실)·`derived: false`(사람 author)·`source`(provenance).
    covers 가 비면 빈 리스트(`covers: []`)로 두고 body 에 TODO placeholder 를 띄운다(아래
    _draft_body). yaml 안전을 위해 title/source 는 따옴표로 감싼다(콜론·특수문자 방어).
    """
    covers_yaml = "[" + ", ".join(covers) + "]" if covers else "[]"
    return (
        f"title: {_yaml_quote(title)}\n"
        f"type: {ptype}\n"
        f"covers: {covers_yaml}\n"
        f"repo: self\n"
        f"derived: false\n"
        f"status: {DRAFT_STATUS}\n"
        f"updated: {today}\n"
        f"source: {_yaml_quote(source)}\n"
    )


def _yaml_quote(value: str) -> str:
    """frontmatter 스칼라를 큰따옴표로 감싼다(콜론·`#` 등 yaml 메타 방어·내부 `"`·`\\` escape)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _draft_body(title: str, covers: list[str], prose: str) -> str:
    """draft 페이지 body(scaffold) — `_template.md` 골격 + prose **verbatim 배치**.

    조사 prose 를 요약/구조화하지 않고 `## 조사 결과` 아래 *그대로* 배치한다(기계는 배치만·
    의미 판단은 PM/LLM). covers 미지정 시 `## 조사 결과` 앞에 `TODO PM: covers 글롭` 을
    띄운다. 한 줄 요약·gotcha·관련 절은 TODO placeholder 로 PM 손을 기다린다.
    """
    covers_todo = "" if covers else "<!-- TODO PM: covers 글롭 (담당 코드) -->\n\n"
    # prose 의 `## ` 헤딩을 `### ` 로 강등 — `## 조사 결과` 절 하위로 일관 배치(페이지 절과 미충돌).
    prose = _demote_prose_headings(prose) if prose.strip() else prose
    prose_block = prose if prose.strip() else "<!-- TODO PM: 조사 prose (--source) -->"
    return (
        f"# {title}\n\n"
        f"## 한 줄\n"
        f"<!-- TODO PM: 한 줄 요약 -->\n\n"
        f"{covers_todo}"
        f"## 조사 결과\n"
        f"{prose_block}\n\n"
        f"## gotcha · 디버깅\n\n"
        f"## 관련\n"
    )


def write_draft_page(title: str, *, ptype: str = DEFAULT_DRAFT_TYPE,
                     covers: list[str] | None = None, slug: str | None = None,
                     source: str | None = None, domain_dir: Path = DOMAIN_DIR,
                     today: str | None = None) -> Path:
    """researcher 조사 prose 를 domain draft 페이지로 scaffold 해 *쓴다*. 경로를 돌려준다.

    파일은 `<domain_dir>/<slug>.draft.md` 에 쓴다(`.draft.md` suffix=가독 보조). frontmatter
    `status: draft` 가 index 제외 진실. **git 은 절대 건드리지 않는다**(add/commit 호출 0 —
    파일만 쓴다). prose 는 `--source` 입력을 그대로 body 에 배치(요약/구조화 금지·기계는 배치만).

    slug 미지정 시 title 에서 도출(slugify)·영숫자 없으면 `draft` 로 대체. covers 미지정 시
    빈 covers + body TODO placeholder. today 미지정 시 오늘 ISO date(`updated`·provenance).
    domain_dir 은 부재 시 생성(scaffold 가 첫 페이지일 수 있음·테스트가 tmp dir 주입).
    """
    covers = covers or []
    slug = slug or slugify(title) or "draft"
    today = today or datetime.date.today().isoformat()
    prose = _read_source(source)
    # source: 라벨 정규화 — repo 내 → 상대경로·stdin/미지정/repo밖(tmp 포함) → placeholder
    # (절대경로/일시경로 박제 금지·promote 후 dangling 방지).
    source_label = _normalize_source_label(source)

    domain_dir = Path(domain_dir)
    domain_dir.mkdir(parents=True, exist_ok=True)
    path = domain_dir / f"{slug}{DRAFT_SUFFIX}"
    frontmatter = _draft_frontmatter(title, ptype, covers, source_label, today)
    body = _draft_body(title, covers, prose)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return path


def cmd_capture_draft(args: argparse.Namespace) -> int:
    """researcher 조사 prose → domain draft 페이지 scaffold (git 무조작·사람 검토 게이트).

    `cmd_capture`(코드→domain·read-only surface)와 **별개** — 이건 조사결과를 draft 페이지로
    *쓴다*(scaffold). 단 frontmatter `status: draft` 라 load_pages 가 index 에서 제외하고,
    git 은 절대 건드리지 않는다(staging 무변화). promote(draft→정식)는 PM 손(status 제거 +
    `.draft.md`→`.md` rename + git add) — 엔진에 promote 명령 부재가 게이트.

    --source 읽기 실패(파일 부재 등)는 명시 에러(stderr·rc 1)로 보고한다 — 잘못된 빈 페이지
    생성을 막는다. 성공 시 생성 경로와 promote 안내를 출력하고 rc 0.
    """
    covers = [c.strip() for c in args.covers.split(",") if c.strip()] if args.covers else []
    try:
        path = write_draft_page(
            args.title,
            ptype=args.type,
            covers=covers,
            slug=args.slug,
            source=args.source,
            domain_dir=DOMAIN_DIR,
        )
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError 는 OSError 하위 아님 — 비-UTF8 source 파일을 명시 에러로(traceback 방지).
        print(f"domain: capture-draft 실패 — source 읽기/쓰기 오류: {exc}", file=sys.stderr)
        return 1
    print(f"draft 생성: {path}")
    print("  status: draft (index 제외 — affected/lint/recall 안 보임).")
    print("  promote(정식화·사람 손): frontmatter status:draft 제거 + 파일명 .draft.md→.md rename + git add.")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """domain freshness lint — stale/orphan/oversized finding 출력 (advisory·항상 exit 0).

    finding 1줄 = `kind · page · detail`. clean 이면 "✓ domain freshness 양호". *비차단* —
    어느 경우도 rc 0(visibility·작업/완료 막지 않음).
    """
    pages = load_pages(DOMAIN_DIR)
    # OVERSIZED_LINES 를 호출 시점에 읽어 명시 전달 — lint_pages 의 기본 인자는 정의
    # 시점에 굳어 monkeypatch(테스트)·재바인딩을 못 본다(cmd_list 의 DOMAIN_DIR 동형).
    findings = lint_pages(pages, oversized_lines=OVERSIZED_LINES)
    if not findings:
        print("✓ domain freshness 양호")
        return 0
    for kind, page, detail in findings:
        print(f"  {kind}  ·  {page}  ·  {detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """domain CLI 파서 (board.py 의 argparse subparsers 패턴)."""
    parser = argparse.ArgumentParser(
        prog="domain",
        description="domain 페이지 covers 인덱스.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="domain 페이지 목록 (title · type · covers · updated)")
    p_list.set_defaults(fn=cmd_list)

    p_affected = sub.add_parser(
        "affected",
        help="ticket touches ∩ domain covers — 영향받는 domain 페이지 (title · covers)",
    )
    target = p_affected.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--ticket", metavar="T-NNNN",
        help="이 ticket 의 frontmatter touches 로 영향 페이지를 찾는다 (board.load_ticket).",
    )
    target.add_argument(
        "--touches", metavar="a,b,c",
        help="콤마분리 경로 목록으로 영향 페이지를 찾는다 (--ticket 대안).",
    )
    p_affected.set_defaults(fn=cmd_affected)

    p_capture = sub.add_parser(
        "capture",
        help="채록 — 세션이 건드린 코드의 담당 페이지(갱신 검토)+coverage gap (read-only·exit 0)",
    )
    cap_target = p_capture.add_mutually_exclusive_group(required=True)
    cap_target.add_argument(
        "--tickets", nargs="+", metavar="T-NNNN",
        help="이 세션 완료 ticket 들 — 각 frontmatter touches 를 집계해 채록 대상을 띄운다. "
             "공백 나열(T-a T-b)·콤마분리(\"T-a,T-b\") 둘 다 수용 (ticket ID 는 공백 무포함이라 무모호).",
    )
    cap_target.add_argument(
        "--touches", metavar="a,b,c",
        help="콤마분리 경로 목록으로 채록 대상을 띄운다 (--tickets 대안).",
    )
    p_capture.add_argument(
        "--all-gaps", action="store_true",
        help="디렉토리 touch의 coverage gap도 접지 않고 전체 표시한다.",
    )
    p_capture.set_defaults(fn=cmd_capture)

    p_draft = sub.add_parser(
        "capture-draft",
        help="researcher 조사 prose → domain 초안 scaffold (status:draft·git 무조작·사람 검토)",
    )
    p_draft.add_argument(
        "--title", required=True, metavar="제목",
        help="draft 페이지 제목 (필수·슬러그 미지정 시 여기서 도출).",
    )
    p_draft.add_argument(
        "--type", default=DEFAULT_DRAFT_TYPE, choices=["concept", "guide", "research"],
        help=f"페이지 type (기본 {DEFAULT_DRAFT_TYPE}).",
    )
    p_draft.add_argument(
        "--covers", metavar="a/**,b/**",
        help="담당 코드 글롭(콤마분리). 미지정 시 빈 covers + 본문 TODO placeholder.",
    )
    p_draft.add_argument(
        "--slug", metavar="kebab",
        help="파일 슬러그(미지정 시 --title 에서 kebab 도출).",
    )
    p_draft.add_argument(
        "--source", metavar="file|-",
        help="조사 prose 입력 — 파일 경로·`-`=stdin·미지정 시 빈 본문(TODO placeholder).",
    )
    p_draft.set_defaults(fn=cmd_capture_draft)

    p_lint = sub.add_parser(
        "lint",
        help="domain freshness lint — stale/orphan/oversized finding (advisory·exit 0)",
    )
    p_lint.set_defaults(fn=cmd_lint)

    return parser


def main(argv: list[str] | None = None) -> int:
    _console_spec = importlib.util.spec_from_file_location(
        "_console_encoding", Path(__file__).resolve().with_name("console_encoding.py")
    )
    _console_encoding = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_encoding)
    _verify_engine_rev(_console_encoding, "console_encoding.py")
    _console_encoding.configure_console_utf8()
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
