"""domain 페이지 파일 읽기·쓰기 (층: infra).

페이지 frontmatter 판독과 디렉터리 전수 적재, 본문 읽기, 스캐폴드 안내 인용, 초안 파일 쓰기를 소유한다.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

from pm_engine.domain import doc_drafts, doc_pages
from pm_engine.infra import fs, ticket_store


# 현재-진실/히스토리 규칙 안내는 스캐폴드 `_template.md` 가 **단일 소유**다. capture-draft 는
# 사람이 그 스캐폴드를 복사하는 경로를 우회해 코드로 페이지를 만들므로, 같은 문구를 여기 두 벌
# 두지 말고 스캐폴드에서 **읽어서** 싣는다(두 벌이면 한쪽만 고쳐 조용히 갈린다).
DOMAIN_TEMPLATE_FILE = "_template.md"
CURRENT_TRUTH_GUIDE_MARKER = "현재-진실 vs 히스토리"


class DomainPageEnumerationError(RuntimeError):
    """페이지 열거 중 특정 문서를 읽거나 파싱하지 못한 오류."""

    def __init__(self, path: Path, cause: Exception):
        self.path = Path(path)
        super().__init__(f"{self.path}: {cause}")


def parse_page(path: Path) -> dict:
    """한 domain 페이지를 파싱한다.

    Returns: {path, title, type, covers: list[str], derived: bool, updated, status,
    verified_at, repo}.
    frontmatter 파싱은 ticket_store.load_ticket 재사용(임의 frontmatter md 파서·DRY).
    covers 부재 → []·derived 부재 → False·status 부재 → None(정식 취급·draft 아님).
    `verified_at` 은 이 페이지 지식이 대조한 검증 기준 커밋 sha(`doc_lint.lint_domain_freshness`
    가 "그 sha 이후 covers 경로 커밋 있나"로 판정) — 부재/비-문자열 → None(freshness skip).
    `repo` 는 그 sha/covers 를 판정할 **소유 저장소 시계**(`self` | `upstream`) — 키 부재만
    `self` 로 자연 퇴화한다. 명시 빈 문자열/false/0/컨테이너는 그대로 보존해 freshness lint 가
    `domain-unverifiable`로 표면화한다.
    frontmatter 깨짐은 호출부가 처리하도록 예외를 그대로 전파한다(load_pages
    가 graceful skip). `status` 는 capture-draft 가 쓴 `draft` 진실 — load_pages 가 이로
    미승인 초안을 index 에서 제외한다(suffix 가 아닌 frontmatter status 가 필터 기준).
    """
    fm, _body = ticket_store.load_ticket(path)
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
    covers = doc_pages._PageCovers(covers, owner_repo)
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


def load_pages(domain_dir: Path) -> list[dict]:
    """domain/ 의 `*.md` 를 **재귀**(rglob) 스캔해 파싱된 페이지 리스트를 돌려준다.

    domain wikitree 를 하위 폴더로 조직해도 그 안의 페이지가 잡히도록 `rglob` 로 재귀
    스캔한다(회사 실사용). README.md·_template.md 는 (어느 깊이든) `name` 으로 제외.
    디렉토리 부재 → [](신규 clone 무영향). 평면 domain/ 은 하위폴더가 없어 결과 불변(additive).

    **frontmatter-less 조용한 skip**: `---` 구분자로 시작하지 않는 `.md`(tmp·메모 등
    다수)는 "페이지 아님" — 개별 경고 없이 조용히 skip 하고 디렉토리별 카운터에만 누적한다.
    스캔 종료 시 skip 이 1개 이상이면 stderr 에 디렉토리별 개수 요약 딱 1줄만 남긴다(파일
    전체 목록 X — LLM 컨텍스트 낭비 원인). 반면 읽기 실패(비 UTF-8 포함)와 `---` 로 시작하는데
    parse 가 깨지는(malformed) 파일은 진짜 페이지 오류라 `DomainPageEnumerationError(path, cause)`
    로 전파한다 — 조회·lint·쓰기 명령 모두 같은 열거 계약이다.

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
        if path.name in doc_pages._NON_PAGE_FILES:
            continue
        # frontmatter 구분자(`---` 시작)가 아예 없는 파일 = 페이지 아님(tmp·메모 등). 개별
        # 경고 없이 조용히 skip 하고 디렉토리별 카운터에만 누적(스캔 종료 시 요약 1줄).
        # 읽기 실패(UnicodeDecodeError 포함)는 판정 불가라 그 경로를 담은 열거 오류로 올린다.
        try:
            has_delimiter = fs.read_text_shared(path, encoding="utf-8").lstrip().startswith("---")
        except (OSError, UnicodeDecodeError) as exc:
            raise DomainPageEnumerationError(path, exc) from exc
        if not has_delimiter:
            key = path.parent.relative_to(domain_dir).as_posix()  # flat → "."
            non_page_counts[key] = non_page_counts.get(key, 0) + 1
            continue
        try:
            page = parse_page(path)
        except Exception as exc:  # noqa: BLE001 — 어떤 파싱 실패든 경로를 담아 변환 재전파.
            raise DomainPageEnumerationError(path, exc) from exc
        if page["status"] == doc_pages.DRAFT_STATUS:
            continue  # 미승인 초안 — index 제외(promote 전까지 안 보임).
        pages.append(page)
    if non_page_counts:
        total = sum(non_page_counts.values())
        breakdown = ", ".join(f"{d}: {n}" for d, n in sorted(non_page_counts.items()))
        print(f"domain: frontmatter 없는 파일 {total}개 skip ({breakdown})", file=sys.stderr)
    return pages


def _page_body(page: dict) -> str:
    """페이지 body(frontmatter 뒤 본문)를 읽는다 — ticket_store.load_ticket 재사용(DRY).

    parse_page 는 frontmatter 만 담으므로 lint(인링크/라인수)용 body 를 따로 읽는다. 읽기/
    파싱 실패는 전파한다.
    """
    _fm, body = ticket_store.load_ticket(page["path"])
    return body


def _read_source(source: str | None) -> str:
    """`--source` 입력(조사 prose)을 읽는다 — `-`=stdin·파일경로=파일·None/(none)=빈 문자열.

    프로비넌스 표기 `(none)`(미지정)도 빈 본문으로 흡수한다. 파일 부재/읽기 실패는 호출부가
    명시 에러로 보고하도록 예외를 전파한다(scaffold 전에 잡혀 잘못된 빈 페이지 생성 방지).
    """
    if source is None or source == "(none)":
        return ""
    if source == "-":
        return sys.stdin.read()
    return fs.read_text_shared(Path(source), encoding="utf-8")


def _normalize_source_label(home, source: str | None) -> str:
    """`--source` 입력을 frontmatter `source:` 에 박을 provenance 라벨로 정규화한다.

    절대경로/일시경로 박제를 막는다(promote 후 dangling). 규칙:
      - stdin(`-`)·미지정(None) → placeholder(자유서술·PM 손).
      - repo 내 파일경로 → **repo 상대경로**(절대경로 아님).
      - repo 밖 경로(일시 `/tmp/...` 포함) → placeholder(절대경로 박제 금지).

    repo 루트 판정은 모듈 REPO 상수(스크립트-위치 앵커) 재사용. repo-내/밖 이분으로 단순화 —
    repo 밖이면 이미 placeholder 라 tmp 별도 패턴 판정 불요(`/tmp/...` 는 repo 밖이므로 흡수됨).
    """
    if source is None or source == "(none)" or source == "-":
        return doc_drafts.SOURCE_TODO_PLACEHOLDER
    try:
        resolved = Path(source).resolve()
        relative = resolved.relative_to(home.root)
    except (ValueError, OSError):
        # ValueError = repo 밖(relative_to 실패) · OSError = resolve 불가 → placeholder.
        return doc_drafts.SOURCE_TODO_PLACEHOLDER
    return relative.as_posix()


def current_truth_guide_block(domain_dir: Path) -> str:
    """`<domain_dir>/_template.md` 의 현재-진실/히스토리 안내 주석 블록 (없으면 빈 문자열).

    스캐폴드가 규칙 문구의 단일 소유고 여기선 **인용만** 한다. 마커를 품은 주석 블록을
    통째로 돌려준다. 템플릿 부재·마커 부재·주석 형태 붕괴는 fail-soft(빈 문자열) — 안내가
    빠질 뿐 draft 생성 자체는 막지 않는다(엔진 동기가 곧 템플릿을 되돌려 놓는다).
    """
    try:
        text = fs.read_text_shared(Path(domain_dir) / DOMAIN_TEMPLATE_FILE, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    marker = text.find(CURRENT_TRUTH_GUIDE_MARKER)
    if marker < 0:
        return ""
    start = text.rfind("<!--", 0, marker)
    end = text.find("-->", marker)
    if start < 0 or end < 0:
        return ""
    return text[start:end + len("-->")]


def write_draft_page(home, title: str, *, ptype: str = doc_drafts.DEFAULT_DRAFT_TYPE,
                     covers: list[str] | None = None, slug: str | None = None,
                     source: str | None = None, domain_dir: Path,
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
    slug = slug or doc_drafts.slugify(title) or "draft"
    today = today or datetime.date.today().isoformat()
    prose = _read_source(source)
    # source: 라벨 정규화 — repo 내 → 상대경로·stdin/미지정/repo밖(tmp 포함) → placeholder
    # (절대경로/일시경로 박제 금지·promote 후 dangling 방지).
    source_label = _normalize_source_label(home, source)

    domain_dir = Path(domain_dir)
    domain_dir.mkdir(parents=True, exist_ok=True)
    path = domain_dir / f"{slug}{doc_drafts.DRAFT_SUFFIX}"
    frontmatter = doc_drafts._draft_frontmatter(title, ptype, covers, source_label, today)
    # 규칙 안내는 스캐폴드에서 파생한다 — 사람이 `_template.md` 를 복사하는 경로만 규칙을 받고
    # 코드 생성 경로(capture-draft)는 못 받던 갭을 같은 문구 한 벌로 닫는다.
    body = doc_drafts._draft_body(title, covers, prose,
                       guide=current_truth_guide_block(domain_dir))
    path.write_text(
        f"---\n{frontmatter}---\n{body}", encoding="utf-8", newline="\n")
    return path
