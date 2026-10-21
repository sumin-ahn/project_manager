"""domain 페이지 lint (층: app).

페이지 stale·고립·크기·이력 누적·coverage 행과 `verified_at` sha 기준 freshness 행을 lint 행으로 낸다.
"""
from __future__ import annotations

from pathlib import Path

from pm_engine.domain import doc_pages
from pm_engine.infra import doc_page_store, pm_paths
from pm_engine.app import doc_coverage, doc_staleness, freshness_lint


# domain lint oversized 임계 — 본문 라인수가 이 값을 넘으면 advisory finding(상수·비차단).
OVERSIZED_LINES = 200


# ── freshness lint (advisory·exit 0·비차단) ──────────────────────
# 페이지를 스캔해 advisory finding 을 낸다(stale/orphan/oversized). **막지 않는다** —
# 전부 exit 0(visibility). unknown(stale==None)은 finding 아님.
# coverage는 아래 freshness 순회와 결과 형식만 공유하며, README marker가 opt-in한 같은
# 채택자 트리에서만 별도 집계한다.

def lint_pages(home, pages: list[dict], *, git_runner: doc_staleness.GitRunner | None = None,
               git_runner_factory: doc_staleness.GitRunnerFactory | None = None,
               oversized_lines: int = OVERSIZED_LINES,
               include_coverage: bool | None = None,
               domain_dir: Path,
               manifest_path: Path) -> list[tuple[str, str, str]]:
    """domain 페이지 스캔 → finding 리스트 `(kind, page, detail)` (advisory·비차단).

    kind ∈ {`stale`, `orphan`, `oversized`, `history`, `coverage`}:
      - **stale** — `page_stale==True`(covers 코드가 updated 후 커밋). unknown(None)은 제외.
      - **orphan** — 다른 domain 페이지에서 이 페이지로의 `[[슬러그]]` 인링크 0(고립). 슬러그
        (파일 stem)와 title 둘 다 인링크로 인정(표기 흔들림 흡수). **자기참조 제외**(자기 body
        의 자기링크는 안 침)·README/_template 은 애초에 load_pages 가 뺀다. **페이지 ≥2 일 때만
        평가** — 1개뿐이면 peer 가 없어 자연 고립이라 skip.
      - **oversized** — body 라인수 > `oversized_lines`(기본 OVERSIZED_LINES=200).
      - **history** — 현재-진실 페이지에 쌓인 변경 이력 항목(`history_leadins`). 크기가 아니라
        *누적 자체*를 본다 — 페이지를 쪼개는 건 증상 유예일 뿐이고, 원인은 현재-진실 문서에
        히스토리를 쓰는 관행이다(현재-진실 doc / 히스토리 구분의 domain 확장).
      - **coverage** — claim marker 의도가 있거나 엔진 coverage 문맥인 README의 수치형
        coverage 산문을 검사한다. 출하 도구 gap·면제·README claim 대조는 같은 트리 README가
        claim marker 의도를 실제로 가질 때만 opt-in한다. marker 없는 채택자의 일반 도메인
        산문은 수치 lint와 vendored 엔진 분모 대조 모두에서 제외한다. 명시
        ``include_coverage=True``도 pages/domain/manifest 트리 일치 검증은 유지한다.

    finding 은 page 표시명(title 우선·없으면 슬러그)으로 라벨한다. clean(빈 리스트)이면
    호출부가 "✓ domain freshness 양호" 를 찍는다. git 은 page_stale 의 DI seam 으로 위임.
    `git_runner_factory`가 있으면 페이지마다 소유 repo runner를 고르고, 없으면 기존 단일
    `git_runner`(테스트/구 호출 호환) 또는 page_stale의 owner 해소를 쓴다.
    """
    findings: list[tuple[str, str, str]] = []

    # 모든 페이지 body 를 한 번 읽어 (a) 전역 인링크 집합·(b) 페이지별 라인수를 모은다.
    bodies = {doc_pages.page_slug(p): doc_page_store._page_body(p) for p in pages}

    # 전역 인링크 집합 — 자기 body 의 자기링크는 제외(self-ref 가 orphan 을 가리지 않게).
    # 한 페이지가 자기를 **슬러그로든 title 로든** 가리킨 링크는 모두 뺀다 — orphan 판정이
    # slug·title 둘 다 인링크로 인정하므로(아래), 자기참조 제외도 slug·title 둘 다여야
    # false-negative 가 없다(`[[자기-title]]` 자기참조가 고립 페이지를 살려내는 갭 차단).
    inlinks: set[str] = set()
    for page in pages:
        slug = doc_pages.page_slug(page)
        self_keys = {slug.lower()}
        title_key = (page["title"] or "").strip().lower()
        if title_key:
            self_keys.add(title_key)
        for target in doc_pages._wikilink_targets(bodies[slug]):
            if target in self_keys:
                continue  # 자기참조(슬러그·title)는 인링크로 안 침.
            inlinks.add(target)

    for page in pages:
        slug = doc_pages.page_slug(page)
        label = page["title"] or slug

        # stale — page_stale==True 만(unknown=None 은 finding 아님).
        page_runner = (git_runner_factory(page) or doc_staleness._UNRESOLVED_GIT_RUNNER
                       if git_runner_factory is not None else git_runner)
        if doc_staleness.page_stale(home, page, git_runner=page_runner) is True:
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

        # history — 시점으로 라벨된 블록(세션 delta·버전 절)이 페이지에 남아 있는가.
        leadins = doc_pages.history_leadins(bodies[slug])
        if leadins:
            findings.append(("history", label, doc_pages._history_detail(leadins)))

    coverage_domain_dir = Path(domain_dir)
    coverage_manifest = Path(manifest_path)
    # marker 삭제/예시 fence 이동으로 거짓 엔진 수치 주장을 숨길 수 없도록 엔진 문맥을 독립
    # 신호로 유지한다. 반면 marker 없는 채택자의 일반 "면제/커버리지" 산문은 오탐하지 않는다.
    readme_body, _readme_error = doc_coverage._coverage_readme_text(coverage_domain_dir)
    numeric_finding = (
        doc_coverage._numeric_coverage_prose_finding(readme_body)
        if readme_body is not None else None
    )
    claim_intent = bool(
        readme_body and doc_coverage.COVERAGE_CLAIM_INTENT_RE.search(readme_body)
    )
    if include_coverage is None:
        # marker 부재는 명시 opt-out이다. 값 오타/중복은 intent regex가 켜서 finding으로
        # 표면화하고, fenced 예시 marker는 제거된 본문이라 opt-in으로 오인하지 않는다.
        include_coverage = claim_intent
    numeric_in_scope = bool(
        claim_intent
        or include_coverage is True
        or (readme_body and doc_coverage._has_engine_coverage_context(readme_body))
    )
    if numeric_finding is not None and numeric_in_scope:
        findings.append(numeric_finding)
    if include_coverage:
        coverage_results = doc_coverage.coverage_findings(
            home,
            pages,
            domain_dir=coverage_domain_dir,
            manifest_path=coverage_manifest,
            exemptions=doc_coverage.DOMAIN_COVERAGE_EXEMPTIONS,
        )
        findings.extend(
            finding for finding in coverage_results
            if finding != numeric_finding
        )
    return findings


# ── 현재-진실 문서 freshness = verified_at sha 판정 (date 비교 대체) ──────────
# architecture.md·status.md·domain 페이지의 freshness 를 frontmatter `verified_at: <sha>`
# 기준으로 판정한다: "그 sha 이후 문서 매핑 경로에 커밋이 있나"의 이진 기계 판정
# Decision 2). date 비교("최신이겠지" 해석 구멍)를 대체한다. architecture.md·
# status.md 는 엔진 코드 트리를, domain 페이지는 covers→pathspec(`doc_staleness` 재사용)을 매핑
# 경로로 쓴다. 판정은 advisory(`_ADVISORY_LINT_KINDS`·never-block·architect 재검증·PM 점검·
# 소유 불변).


def lint_domain_freshness(home, *, runner=None) -> list[tuple[str, str, str]]:
    """domain 페이지 freshness advisory — `verified_at` sha 판정 (never-block).

    각 domain 페이지(`covers:` 보유)의 frontmatter `verified_at: <sha>` *이후* 그 페이지
    covers 경로에 커밋이 있으면 `domain-stale` 권고. **경로 매핑은 `doc_staleness` 재사용**(신설 0):
    페이지 covers 글롭 → `doc_staleness.covers_pathspecs`(**HEAD 커밋 트리 기준 존재/부재 분할** —
    `git diff <빈-트리> HEAD`·index/staged 무관)로 git pathspec 을 만든다 — 별도 매핑 저장소를
    두지 않는다. 커밋-기준 관찰가능성이라 untracked/staged-only
    생성물은 부재로, sparse checkout tracked 는 존재로 정직히 분류한다(
    `Path.exists`·index 오판 회피).

    **관찰불가 사각을 `domain-unverifiable` advisory 로 표면화한다**(거짓 green 근절·
    판정을 억지로 만들지 않고 *검증 불가*임을 정직히 알림):
      - **anchor 판정 선행**(`_sha_anchor_status`): `verified_at` 이 유효 anchor(고정 hex+해소+
        HEAD 선조)가 아니면 형식 아님/미해소(타-git SHA)/비-선조(descendant·딴 브랜치) 사유로
        advisory — `<sha>..HEAD` range 자체가 성립 안 하니 covers 관찰가능성 판정은 하지 않는다.
      - **covers 관찰가능성**(anchor OK 일 때·`covers_pathspecs`·pin 경계): 미추적 + pin 이후
        델타 0 (never-tracked=`templates/**` 또는 pin *이전* 삭제) → `absent` → advisory.
        현재 tracked·pin 이후 삭제/rename → `present` → `<sha>..HEAD` stale 검사로.
    **소유 저장소 시계**: 페이지 `repo:`가 `self`(부재 기본)면 REPO, `upstream`이면
    `local.conf upstream.path=`의 경로형 checkout 에서 anchor·covers·delta를 모두 판정한다. 문서 위치는
    REPO에 그대로 두고 git 조회만 소유 repo로 바꾼다. upstream 미설정/URL/이동은
    `domain-unverifiable` advisory 이며 green 으로 흡수하지 않는다.

    advisory 는 never-block(`_ADVISORY_LINT_KINDS`)이라 `--gate` 종료코드에 기여하지 않는다.

    fail-soft: 페이지 스캔 실패·페이지 없음·verified_at 부재·covers 빈·git 불가(sha
    해소 None) → [](명시 skip·신규 clone 무영향). `updated` date 기반 `lint_domain`
    stale 과는 별개 축(이건 sha 기준)."""
    try:
        pages = doc_page_store.load_pages(pm_paths.domain_dir(home))
    except Exception:  # noqa: BLE001 — 스캔 실패는 [] 로 흡수(board lint 정상 진행).
        return []
    findings: list[tuple[str, str, str]] = []
    _UNVERIFIABLE_PREFIX = ("이 저장소에서 freshness 검증 불가(소유 페이지일 수 있음·소유 "
                            "repo 에서 재검증) — ")
    for page in pages:
        sha = str(page.get("verified_at") or "").strip()
        if not sha:
            continue  # verified_at 미부여 페이지 — 명시 skip(false-green 아님).
        covers = page.get("covers") or []
        # covers 가 전부 빈/공백(코드-무관 개념)이면 sha 판정 없이 skip — git 무접촉. 비-공백 패턴은
        # 하나라도 있으면 판정한다(미지원 형태는 아래 covers_pathspecs 가 unmappable advisory 로;
        # `covers_glob_pathspec` 로 게이트하면 미지원-only 페이지가 조용히 skip 돼 false-green).
        if not any(str(g).strip() for g in covers):
            continue
        label = page.get("title") or doc_pages.page_slug(page)
        owner_repo, owner_error = freshness_lint._freshness_owner_repo(home, page.get("repo"))
        if owner_repo is None:
            findings.append((
                label, "domain-unverifiable",
                _UNVERIFIABLE_PREFIX + f"소유 repo 미해소({owner_error})"))
            continue
        status, full_oid = freshness_lint._sha_anchor_status(home, sha, runner=runner, repo=owner_repo)
        if status == freshness_lint._ANCHOR_UNKNOWN:
            continue  # git 부재 등 환경적 판정불가 — 종전 fail-soft silent skip 유지.
        if status != freshness_lint._ANCHOR_OK:
            # sha 자체가 유효 anchor 아님 — `<oid>..HEAD` range 불가라 covers 관찰가능성 판정을
            # 하지 않는다(전제: 미해소 페이지는 pathspec 판정에 안 온다). 사유만 advisory.
            findings.append((label, "domain-unverifiable",
                             _UNVERIFIABLE_PREFIX + freshness_lint._unverifiable_sha_reason(status, sha)))
            continue
        # status OK — **canonical full OID** 를 pin 으로 관찰가능성/stale 판정(원 입력 재해석 제거·
        # :(glob)). covers_pathspecs 의 `<pin>..HEAD` 도 full OID 경유.
        present, absent, unmappable = doc_staleness.covers_pathspecs(
            home, covers, repo=owner_repo, git_runner=runner, verified_at=full_oid)
        reasons: list[str] = []
        if absent:
            reasons.append(
                f"covers 경로가 pin 이후 관찰 불가({', '.join(absent)}·pin 이전 삭제)")
        if unmappable:
            # 미지원 문법(비-경계 **)·repo 밖/절대 경로 등 두 방언 동일성 미보장 → 오번역 대신 표면화.
            reasons.append(
                f"covers 패턴 미지원(형태·repo 밖·절대 경로): {', '.join(unmappable)}")
        if reasons:
            findings.append((label, "domain-unverifiable",
                             _UNVERIFIABLE_PREFIX + " · ".join(reasons)))
        # present 글롭을 :(glob) magic pathspec 으로 넘겨 full OID 기준 stale 검사(접두사 손실 없이).
        present_specs = [doc_pages.covers_glob_pathspec(g) for g in present]
        if present_specs and freshness_lint._git_commits_between(
                home,
                full_oid, present_specs, runner=runner, repo=owner_repo) is True:
            findings.append((
                label, "domain-stale",
                f"verified_at({sha[:12]}) 이후 covers 경로에 커밋 있음 — 페이지 재검증 필요"))
    return findings


def lint_domain(home) -> list[tuple[str, str, str]]:
    """domain freshness finding 을 board lint finding 으로 표면화 (advisory·비차단).

    `lint_pages` 의 `(kind, label, detail)` 를 board 관례 `(label, kind, detail)` 로
    재배열해 돌려준다. kind 는 domain 의 `stale`/`orphan`/`oversized`/`history` 를 보존 —
    `_ADVISORY_LINT_KINDS` 에 등재돼 `--gate` 종료코드에 *절대* 기여하지 않는다(visibility>
    enforcement). 깨진 페이지·git 부재 → [] (domain 미사용
    프로젝트 무영향). `lint_pages` 가 이미 graceful 이므로 얇게 위임하되, 어떤 예외도 [] 로
    흡수해 board lint 자체는 항상 정상 진행한다.

    date freshness도 페이지별 `repo:` 소유 시계로 판정한다. owner checkout별 runner를
    캐시해 같은 repo 페이지끼리 공유하고, 소유 repo 미해소/runner 생성 실패는 그 페이지 stale을
    unknown으로 둔다(sha freshness 축은 별도 `domain-unverifiable` advisory로 표면화).
    """
    try:
        pages = doc_page_store.load_pages(pm_paths.domain_dir(home))
        runners: dict[Path, object] = {}

        def git_runner_factory(page):
            owner_repo, _owner_error = freshness_lint._freshness_owner_repo(home, page.get("repo"))
            if owner_repo is None:
                return None
            owner_repo = Path(owner_repo)
            if owner_repo not in runners:
                runners[owner_repo] = doc_staleness._real_git_runner(owner_repo)
            return runners[owner_repo]

        findings = lint_pages(
            home, pages, git_runner_factory=git_runner_factory,
            domain_dir=pm_paths.domain_dir(home), manifest_path=pm_paths.engine_manifest(home),
        )
    except Exception:  # noqa: BLE001 — 어떤 실패도 빈 결과로 흡수(board lint 정상 진행).
        return []
    # domain (kind, label, detail) → board (label, kind, detail) 재배열.
    return [(label, kind, detail) for kind, label, detail in findings]
