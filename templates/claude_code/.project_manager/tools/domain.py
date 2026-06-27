#!/usr/bin/env python3
"""domain 레이어 — 페이지 covers 파서 + 코드↔페이지 인덱스 (ADR-0018 · Phase 1 #1).

`domain/` 의 각 페이지 frontmatter `covers:`(담당 코드 글롭)를 파싱해 *페이지 ↔ 코드*
인덱스를 만든다. 이게 이후 touches∩covers 매칭(#2)·staleness(#3)·소환(Phase 2)의 토대다.

범위: 파서(`parse_page`)·스캔(`load_pages`)·매칭(`pages_for_path`)·touches∩covers
(`pages_for_touches`·#2)·staleness(`page_stale`·#3)·freshness lint(`lint_pages`)·
CLI(`list`/`affected`/`capture`/`capture-draft`/`lint`). capture(채록·`uncovered_paths`
gap·Phase 3)는 surface-only — 담당 페이지·coverage gap 을 *띄울* 뿐 본문 자동 생성/스탬프는
안 한다(자동 `updated:` 는 stale 탐지를 거짓으로 만듦·ADR-0018 결정). capture-draft(T-0167·
Phase 2)는 researcher 조사 prose 를 domain 초안(`status: draft`)으로 *scaffold* 한다 —
prose 는 verbatim 배치(요약/구조화 금지)·**git 무조작**(add/commit 0)·promote(draft→정식)는
사람 손. 범위 밖(후속): derive(코드서 자동 채록·`derived:true`·Phase 5)·contradiction(LLM).

설계 (ADR-0018):
  - **frontmatter 파싱은 board.load_ticket 재사용** — 이름은 ticket 이나 임의 frontmatter md
    파서다(board.py:714). 중복 파서 정의 금지(DRY·codex reuse 강조). board 는 같은 tools/
    에서 `_load_module`(spec_from_file_location) 로 로드한다 — 패키지 설치 없이 동작하는
    board.py·pm_*.py 와 같은 로드 규약.
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
import datetime
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py·pm_config.py 와 동일
# 앵커 규약(어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / ".project_manager" / "tools"
DOMAIN_DIR = REPO / ".project_manager" / "wiki" / "domain"

# load_pages 가 스킵하는 비-페이지 파일(인덱스 README·복사용 템플릿).
_NON_PAGE_FILES = frozenset({"README.md", "_template.md"})

# domain lint oversized 임계 — 본문 라인수가 이 값을 넘으면 advisory finding(상수·비차단).
OVERSIZED_LINES = 200

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

# git log 커밋 날짜 포맷(ISO 8601·`%cI` = strict ISO·`2026-06-19T07:59:00+09:00`).
_GIT_LOG_FORMAT = "--format=%cI"

GIT_TIMEOUT_SECONDS = 120


# ── 엔진 모듈 동적 로드 (스크립트-위치 앵커·board.py·pm_config.py 선례) ──────────
# board.py 는 같은 tools/ 에 있다. spec_from_file_location 으로 로드한다 — 패키지 설치
# 없이 동작(board.py·pm_*.py 와 같은 로드 규약). 부재/실패는 명시 에러로 보고한다.


def _load_module(name: str, filename: str):
    """tools/<filename> 를 모듈로 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    path = TOOLS_DIR / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 호출부가 명시 에러로 보고.
        return None


def _load_board():
    """board 모듈을 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    return _load_module("board", "board.py")


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


# ── staleness (git 기반·covers→pathspec·fail-soft) ───────────────────────────
# 페이지 `covers` 코드가 페이지 `updated` *후* git 커밋된 적 있으면 stale = "페이지 지식이
# 코드보다 뒤처졌을 수 있다"(ADR-0018 Q3). enforcement 아닌 visibility — ⚠ 표시·lint
# advisory 만(막지 않음). 판정불가는 전부 None(fail-soft·"unknown") — git 부재(솔로/CI)·
# 에러·covers 빈·updated 파싱 실패에 crash 0.


def _real_git_runner(cwd: Path) -> GitRunner:
    """실 git 을 `cwd` 컨텍스트로 호출하는 GitRunner (worktree_pool._real_git_runner 패턴).

    반환 callable: argv(list) → (returncode, stdout). git 바이너리 부재(shutil.which)면
    (1, msg)·예외(타임아웃 등)는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부 위임·
    raise 안 함). `git -C <cwd> <argv...>` 로 항상 그 repo 에 묶는다. 엔진 규약대로
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


def covers_to_pathspec(glob: str) -> str | None:
    """covers 글롭을 git pathspec(리터럴 prefix)으로 변환한다 — **보수적 over-approx**.

    git 은 우리 글롭 시맨틱(`**`)을 모르므로 **첫 와일드카드(`*`) 전 리터럴 prefix** 만
    취해 git log pathspec 으로 쓴다. 디렉토리 전체를 가리켜 *과경고 < 미경고* 쪽이다
    (디렉토리 안 다른 파일 커밋도 stale 신호 — "검토해" 라 안전·docstring 명기).
      - `src/analysis/**` → `src/analysis`
      - `src/*.py`        → `src`
      - `a/b.py`          → `a/b.py`(와일드카드 없음 = 글롭 전체가 리터럴)
    **첫 와일드카드 전 prefix 가 비면(글롭이 `**`·`*…` 로 시작) None** — pathspec 으로 못
    좁혀 그 글롭은 skip(빈 pathspec=repo 전체라 무의미·page_stale 이 건너뛴다).
    """
    star = glob.find("*")
    prefix = glob if star == -1 else glob[:star]
    # 첫 와일드카드 전까지 자른 prefix 의 trailing 슬래시 제거(`src/analysis/` → `src/analysis`).
    prefix = prefix.rstrip("/")
    return prefix or None


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


def page_stale(page: dict, *, git_runner: GitRunner | None = None) -> bool | None:
    """페이지 covers 코드가 페이지 `updated` *후* git 커밋된 적 있으면 stale (ADR-0018 #3).

    `True` = stale(최신 covers 커밋 날짜 > updated)·`False` = fresh(커밋이 updated 이하)·
    **`None` = 판정불가(fail-soft·unknown)**. None 이 되는 경우:
      - covers 가 비었다(코드-무관 개념 — 평가 대상 없음).
      - `updated` 파싱 실패(부재·깨짐).
      - covers 글롭들이 전부 빈 pathspec(글롭이 `**`/`*…` 로 시작 — 좁힐 prefix 없음).
      - git 호출 실패(rc≠0·git 부재/에러) 또는 빈 출력(미추적·커밋 0).
    crash 0 — git 없는 환경(솔로/CI)도 무탈히 unknown.

    **보수적 pathspec**(covers_to_pathspec): 글롭의 리터럴 prefix(디렉토리)로 over-approx
    해 *과경고 < 미경고* 쪽이다(stale 을 덜 놓침). covers 의 여러 글롭은 하나의 `git log
    -1 -- <pathspec…>` 로 합쳐 *그 중 가장 최근* 커밋 날짜를 본다(어느 covers 코드든 바뀌면
    stale). git 은 주입 `git_runner`(DI seam·테스트 hermetic) 또는 실 subprocess(미주입).
    """
    covers = page.get("covers") or []
    if not covers:
        return None

    updated_date = _parse_updated_date(page.get("updated"))
    if updated_date is None:
        return None

    pathspecs = [ps for ps in (covers_to_pathspec(g) for g in covers) if ps]
    if not pathspecs:
        return None

    runner = git_runner or _real_git_runner(REPO)
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


def parse_page(path: Path) -> dict:
    """한 domain 페이지를 파싱한다.

    Returns: {path, title, type, covers: list[str], derived: bool, updated, status}.
    frontmatter 파싱은 board.load_ticket 재사용(임의 frontmatter md 파서·DRY).
    covers 부재 → []·derived 부재 → False·status 부재 → None(정식 취급·draft 아님).
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
    return {
        "path": path,
        "title": fm.get("title") or "",
        "type": fm.get("type") or "",
        "covers": covers,
        "derived": bool(fm.get("derived")),
        "updated": fm.get("updated"),
        "status": status,
    }


def load_pages(domain_dir: Path = DOMAIN_DIR) -> list[dict]:
    """domain/ 의 `*.md` 를 **재귀**(rglob) 스캔해 파싱된 페이지 리스트를 돌려준다.

    domain wikitree 를 하위 폴더로 조직해도 그 안의 페이지가 잡히도록 `rglob` 로 재귀
    스캔한다(T-0126·회사 실사용). README.md·_template.md 는 (어느 깊이든) `name` 으로 제외.
    frontmatter 없는/깨진 파일은 graceful skip(stderr 경고·crash 0). 디렉토리 부재 → []
    (solo·신규 clone 무영향). 평면 domain/ 은 하위폴더가 없어 결과 불변(additive).

    **draft 제외(T-0167)**: frontmatter `status == "draft"` 페이지는 미승인 초안
    (capture-draft scaffold)이라 index 에서 뺀다 — affected/lint/recall/capture 가
    승인 안 된 지식을 보지 않게. 필터 기준은 frontmatter status 이지 `.draft.md` 파일명이
    아니다(promote = status:draft 제거 1개로 비로소 정식 = 포함). status 부재/기타 → 포함.
    """
    domain_dir = Path(domain_dir)
    if not domain_dir.is_dir():
        return []
    pages: list[dict] = []
    for path in sorted(domain_dir.rglob("*.md")):
        if path.name in _NON_PAGE_FILES:
            continue
        try:
            page = parse_page(path)
        except Exception as exc:  # noqa: BLE001 — 깨진 페이지는 skip(경고만·crash 금지).
            print(f"domain: {path.name} 파싱 skip — {exc}", file=sys.stderr)
            continue
        if page["status"] == DRAFT_STATUS:
            continue  # 미승인 초안 — index 제외(promote 전까지 안 보임).
        pages.append(page)
    return pages


def pages_for_path(path: str, pages: list[dict]) -> list[dict]:
    """주어진 repo-relative 코드 경로를 covers 글롭으로 담는 페이지들을 돌려준다.

    경로 구분자를 POSIX(`/`)로 정규화해 매칭한다(Windows 백슬래시 무관). os.sep 뿐
    아니라 백슬래시도 직접 치환해 POSIX 실행 중 들어온 Windows 경로도 정규화한다. 빈
    covers 페이지(코드-무관 개념)는 어떤 경로도 매치하지 않는다.
    """
    norm = path.replace(os.sep, "/").replace("\\", "/")
    return [page for page in pages if _path_matches_covers(norm, page["covers"])]


def uncovered_paths(touches: list[str] | None, pages: list[dict] | None = None) -> list[str]:
    """touch 경로 중 *어느 페이지 covers 글롭에도 안 잡힌* 것들을 돌려준다 (coverage gap·ADR-0018 §7b).

    capture(채록)의 gap 검출 — touched 코드인데 담당 domain 페이지가 없는 경로 = 후보
    신규 페이지. 각 touch 에 `pages_for_path`(매칭 로직 재사용·DRY)를 적용해 매칭 0 인 것만
    남긴다. **발견 순서 보존·dedup**(같은 경로가 touches 에 중복돼도 한 번만). 비-문자열
    touch·빈/공백 경로는 방어적으로 건너뛴다(`pages_for_touches` 동형). 빈/None touches → [].

    `pages` 미주입 시 `load_pages()`(실 domain/ 스캔·부재 시 []). domain/ 가 비면 *모든*
    touch 가 uncovered (담당 페이지 0) — solo·신규 clone 무영향(capture 가 gap 절을 띄움).
    """
    if not touches:
        return []
    if pages is None:
        # cmd_*·pages_for_touches 동형 — 호출 시점의 모듈 전역 DOMAIN_DIR 을 읽는다.
        pages = load_pages(DOMAIN_DIR)
    seen: set[str] = set()
    out: list[str] = []
    for touch in touches:
        if not isinstance(touch, str):
            continue
        norm = touch.strip()
        if not norm:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        if not pages_for_path(norm, pages):
            out.append(norm)
    return out


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
        norm = touch.strip()
        if not norm:
            continue
        for page in pages_for_path(norm, pages):
            key = page["path"]
            if key in seen:
                continue
            seen.add(key)
            out.append(page)
    return out


# ── freshness lint (advisory·exit 0·비차단·ADR-0018 #3) ──────────────────────
# 페이지를 스캔해 advisory finding 을 낸다(stale/orphan/oversized). **막지 않는다** —
# 전부 exit 0(visibility·Q3). unknown(stale==None)은 finding 아님.

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
               oversized_lines: int = OVERSIZED_LINES) -> list[tuple[str, str, str]]:
    """domain 페이지 스캔 → finding 리스트 `(kind, page, detail)` (advisory·비차단).

    kind ∈ {`stale`, `orphan`, `oversized`}:
      - **stale** — `page_stale==True`(covers 코드가 updated 후 커밋). unknown(None)은 제외.
      - **orphan** — 다른 domain 페이지에서 이 페이지로의 `[[슬러그]]` 인링크 0(고립). 슬러그
        (파일 stem)와 title 둘 다 인링크로 인정(표기 흔들림 흡수). **자기참조 제외**(자기 body
        의 자기링크는 안 침)·README/_template 은 애초에 load_pages 가 뺀다. **페이지 ≥2 일 때만
        평가** — 1개뿐이면 peer 가 없어 자연 고립이라 skip(T-0097).
      - **oversized** — body 라인수 > `oversized_lines`(기본 OVERSIZED_LINES=200).

    finding 은 page 표시명(title 우선·없으면 슬러그)으로 라벨한다. clean(빈 리스트)이면
    호출부가 "✓ domain freshness 양호" 를 찍는다. git 은 page_stale 의 DI seam 으로 위임.
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
        if page_stale(page, git_runner=git_runner) is True:
            findings.append(("stale", label, f"covers 코드가 updated({page['updated']}) 후 커밋됨"))

        # orphan — 슬러그/title 어느 표기로도 인링크 0.
        # 단 페이지가 1개뿐이면 orphan 판정 skip — 인링크할 *peer 가 존재하지 않아* 자연
        # 고립이고(첫 페이지는 항상 orphan), 매 lint 마다 의미 없는 advisory 가 떠 "clean"
        # 시그널을 흐린다. orphan 은 peer(≥2 페이지)가 있을 때만 의미 있다 (T-0097·T-0094 reviewer 권고).
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
    무표시(노이즈 방지·ADR-0018 Q3). 마커 폭(2칸)을 비-stale 줄에도 채워 줄을 정렬한다.
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


def cmd_capture(args: argparse.Namespace) -> int:
    """세션이 건드린 코드의 담당 domain 페이지를 "갱신 검토" 대상으로 띄운다 (채록·ADR-0018 §7b).

    recall(T-0083)의 쓰기 측 짝 — *무엇을 갱신/신설할지* 띄울 뿐 본문을 자동 생성/스탬프하지
    않는다(surface-only·자동 `updated:` 금지 → stale 탐지 거짓 방지·결정 절). 두 절 출력:
      1. **영향 페이지** — touches ∩ covers 매칭(`pages_for_touches`) + `⚠ ` stale 마커(T-0082).
      2. **coverage gap** — 어느 페이지 covers 에도 안 잡힌 touch 경로(`uncovered_paths`)
         = 후보 신규 페이지. 비면 절 생략.
    둘 다 없으면 `(채록할 domain 변화 없음)`. **read-only·항상 exit 0**(advisory·작업 무차단).
    """
    if args.tickets:
        touches = _touches_from_tickets(args.tickets)
    else:
        # --touches a,b,c — affected 동형(콤마분리·공백 trim·빈 토큰 제거).
        touches = [t.strip() for t in args.touches.split(",") if t.strip()]

    pages = load_pages(DOMAIN_DIR)
    affected = pages_for_touches(touches, pages)
    gaps = uncovered_paths(touches, pages)

    if not affected and not gaps:
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
        for path in gaps:
            print(f"  {path}")
    return 0


# ── capture-draft (researcher 조사 prose → domain 초안 scaffold·git 무조작·T-0167) ──
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
    어긋난다(PM 40 dogfood). `## `(정확히 H2)만 `### ` 로 강등해 scaffold 절 하위로 일관 배치.

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

    절대경로/일시경로 박제를 막는다(promote 후 dangling·PM 40 dogfood). 규칙:
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
    # (절대경로/일시경로 박제 금지·promote 후 dangling 방지·PM 40 dogfood).
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
    어느 경우도 rc 0(visibility·ADR-0018 Q3·작업/완료 막지 않음).
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
        description="domain 페이지 covers 인덱스 (ADR-0018).",
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
        "--tickets", metavar="T-a,T-b",
        help="이 세션 완료 ticket 들 — 각 frontmatter touches 를 집계해 채록 대상을 띄운다.",
    )
    cap_target.add_argument(
        "--touches", metavar="a,b,c",
        help="콤마분리 경로 목록으로 채록 대상을 띄운다 (--tickets 대안).",
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


def _reconfigure_console() -> None:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # em-dash(·)·한글 print 가 UnicodeEncodeError 로 죽는 것을 막는다(board.py 동형).
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    _reconfigure_console()
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
