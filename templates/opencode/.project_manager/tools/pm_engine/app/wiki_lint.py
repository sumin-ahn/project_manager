"""wiki 운영 문서 lint (층: app).

status.md 완료 행 누적, family scope 태그, ADR 수명주기 back-ref 정합과 작성자 표기를 판정한다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from pm_engine.infra import areas_store, fs, pm_paths, ticket_store


# status.md ✅ 완성 행 누적 임계값 (warn-only — 차단 아님·archive 권고). 활성 매트릭스 = 진행 중만.
# 남은 가드는 ✅ 누적뿐.
STATUS_DONE_ROW_WARN = 30

# 모듈 매트릭스 행 중 상태 셀이 ✅ 인 행 (범례 "- ✅ ..." 는 `|` 시작 아니라 제외).
_STATUS_DONE_ROW_RE = re.compile(r"^\|.*\| ✅ \|", re.MULTILINE)


def lint_status(home) -> list[tuple[str, str, str]]:
    """status.md 의 ✅ 완성 행 누적을 경고한다 (warn-only·judgment-only status).

    Checks:
      - status-done-accum: 활성 매트릭스에 ✅ 완성 행이 누적 — status_done.md 로 archive 권고.

    (status.md 헤더 scalar·테스트 수·합계·소계·회귀 실측은 derivable 이라
    제거됐다 — 따라서 `status-header-bloat` 가드와 ticket_finish 스칼라 앵커 무결성 검사
    `lint_status_anchors` 도 같이 제거. status.md = judgment-only.)

    status.md 없으면 빈 리스트. (board.py refresh/lint 끝에서 호출.)
    """
    issues: list[tuple[str, str, str]] = []
    if not pm_paths.status_file(home).exists():
        return issues
    text = fs.read_text_shared(pm_paths.status_file(home), encoding="utf-8")

    done_rows = len(_STATUS_DONE_ROW_RE.findall(text))
    if done_rows > STATUS_DONE_ROW_WARN:
        issues.append((
            "status.md", "status-done-accum",
            f"활성 매트릭스 ✅ 완성 행 {done_rows}개 > {STATUS_DONE_ROW_WARN} — "
            f"status_done.md 로 archive 권고"))

    return issues


FAMILY_SCOPE_DEFAULT = "shared"  # family_scope 부재/빈값 → shared 로 간주.

# family_scope 가 인지되는 wiki 디렉토리 — ADR(decisions/)·spec(specs/).
def _scope_aware_dirs(home) -> tuple[Path, ...]:
    return (pm_paths.decisions_dir(home), pm_paths.specs_dir(home))


# 유효 family_scope 값 형식 — `shared` 또는 prefix 형(영숫자·`-`·`_`, 등록 prefix 와 동형).
# 형식만 검사(등록 여부는 advisory 메시지로) — areas.md 부재에서도 동작.
_FAMILY_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _in_scope_aware_dir(home, path: Path) -> bool:
    """path 가 scope-aware 디렉토리(decisions/·specs/) 안에 있는가.

    promote-scope 가 ADR/spec 문서로만 retag 를 제한하기 위한 가드— 임의
    frontmatter 문서 retag 를 거부한다. 경로 비교 전 resolve 된 절대경로를 받는다.
    """
    resolved = path if path.is_absolute() else (home.root / path).resolve()
    for base in _scope_aware_dirs(home):
        try:
            base_resolved = base.resolve()
        except OSError:
            continue
        if resolved == base_resolved or base_resolved in resolved.parents:
            return True
    return False


def family_scope(fm: dict[str, Any]) -> str:
    """frontmatter dict 의 family wiki scope. 부재/빈값 → shared.

    `family_scope:` 값을 strip 해 반환한다. 없거나 빈 문자열이면 `shared` 기본
    비-문자열(잘못 적힌 list 등)도 shared 로
    안전 폴백 — 파싱이 절대 예외를 던지지 않게 한다(lint fail-soft).
    """
    raw = fm.get("family_scope")
    if not isinstance(raw, str):
        return FAMILY_SCOPE_DEFAULT
    val = raw.strip()
    return val or FAMILY_SCOPE_DEFAULT


def lint_scopes(home) -> list[tuple[str, str, str]]:
    """family_scope 태그를 파싱·인지한다 (kind=`scope-advice`·자문성).

    decisions/·specs/ 문서의 `family_scope:` 를 읽어 *인지*한다(부재 → shared 기본).
    차단은 최소 — 다음만 자문성 권고(never-blocks·`_ADVISORY_LINT_KINDS`):
      - 비문자열 family_scope (list/dict/number 등 — frontmatter 형식 오류).
      - 형식이 깨진 scope (공백/특수문자 등 `_FAMILY_SCOPE_RE` 불일치).
      - shared 도 아니고 areas.md 의 등록 prefix 도 아닌 미지의 repo scope (오타 신호).
        단 areas.md 부재면 등록 대조를 건너뛴다 — 미등록에서 repo scope 는 미래값일 뿐.
    scope 자체로 hard-fail 을 만들지 않는다.
    (family_scope 부재) 에선 항상 빈 리스트 — 회귀 0.

    *원본 값* 을 검사한다(파싱 헬퍼 `family_scope()` 의 fail-soft 폴백과 분리) — 헬퍼는
    비문자열을 shared 로 안전 폴백하지만, lint 는 그 형식 오류를 조용히 삼키지 않고
    `scope-advice` 로 권고해야 한다.
    """
    issues: list[tuple[str, str, str]] = []
    known = areas_store.registered_prefixes(home)  # areas.md 부재면 빈 set → 등록 대조 생략.
    for base in _scope_aware_dirs(home):
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.md")):
            try:
                fm, _body = ticket_store.load_ticket(p)
            except (ValueError, OSError, yaml.YAMLError):
                # frontmatter 없음(README) 또는 placeholder({{DATE}} 등 비-YAML) — scope 인지
                # 대상 아님(fail-soft). 한 문서 파싱 실패가 lint 전체를 막지 않게 흡수한다.
                continue
            if not isinstance(fm, dict):
                continue  # frontmatter 가 스칼라/리스트 — scope 인지 대상 아님.
            if "family_scope" not in fm:
                continue  # 부재 = shared 기본 — 정상, 보고 없음.
            raw = fm.get("family_scope")
            src = pm_paths._rel_to_repo(home, p)
            if not isinstance(raw, str):
                # list/dict/number 등 — frontmatter 형식 오류. 헬퍼는 shared 폴백하지만
                # lint 는 조용히 삼키지 않고 권고한다.
                issues.append((src, "scope-advice",
                               f"family_scope 가 비문자열({type(raw).__name__}) — "
                               f"`shared` 또는 repo prefix 문자열이어야 함"))
                continue
            if not raw.strip():
                continue  # 빈값 = shared 기본 — 정상, 보고 없음.
            scope = raw.strip()
            if not _FAMILY_SCOPE_RE.match(scope):
                issues.append((src, "scope-advice",
                               f"family_scope={scope!r} 형식이 깨짐 — `shared` 또는 "
                               f"repo prefix 여야 함"))
            elif scope != FAMILY_SCOPE_DEFAULT and known and scope not in known:
                issues.append((src, "scope-advice",
                               f"family_scope={scope!r} 가 등록된 repo prefix 아님 "
                               f"(areas.md: {sorted(known)}) — 오타 또는 승격 누락 가능 "
                               f""))
    return issues


def _adr_id_from_path(p: Path) -> str:
    """decisions/ 파일명(`NNNN-slug.md`) → `ADR-NNNN`. `.stem` 으로 확장자 제거(dashless 방어)."""
    return f"ADR-{p.stem.split('-', 1)[0]}"


def _as_id_list(val) -> list[str]:
    """frontmatter 값(None/str/list)을 ID 문자열 리스트로 정규화한다.

    `amends: [ADR-NNNN, ADR-NNNN]`(yaml list) · `amends: ADR-NNNN`(scalar) ·
    `refines: ADR-NNNN, ADR-NNNN`(comma scalar) 모두 수용 — 쉼표/공백 분리.
    """
    if val is None:
        return []
    items = val if isinstance(val, list) else re.split(r"[,\s]+", str(val))
    return [str(s).strip() for s in items if str(s).strip()]


def lint_adr_lifecycle(home) -> list[tuple[str, str, str]]:
    """ADR lifecycle 정합 advisory (never-block).

    `amends:[Y]`/`supersedes:Y` 인 ADR-X 에 대해 대상 Y(ADR)가 **back-ref**(amended_by/
    superseded_by 에 X)와 **status**(amended/superseded)를 갖는지 검사한다. + 자가일관:
    status=amended 면 amended_by 가, superseded 면 superseded_by 가 있어야 한다. 어긋나면
    권고(kind=`adr-lifecycle`·`_ADVISORY_LINT_KINDS` 등재로 `--gate` 종료코드 비기여).
    `refines`(추가·대상 불변)는 검사 안 한다. ticket back-ref(`amended_by:[T-NNNN]`)는
    forward edge 가 없어 cross-check 대상 아님(자가일관만). decisions/ 부재·깨진 frontmatter
    → graceful skip(신규 clone·ADR 0개 무영향)."""
    findings: list[tuple[str, str, str]] = []
    if not pm_paths.decisions_dir(home).is_dir():
        return findings
    adrs: dict[str, dict] = {}
    for p in sorted(pm_paths.decisions_dir(home).glob("[0-9]*.md")):
        try:
            fm, _ = ticket_store.load_ticket(p)
        except ticket_store._load_ticket_soft_errors():  # 깨진/frontmatter 없는 파일은 skip(비차단).
            continue
        adrs[_adr_id_from_path(p)] = fm or {}

    for adr_id, fm in adrs.items():
        status = str(fm.get("status") or "").strip()
        for verb, want_status, back_field in (
            ("amends", "amended", "amended_by"),
            ("supersedes", "superseded", "superseded_by"),
        ):
            for tgt in _as_id_list(fm.get(verb)):
                if not tgt.startswith("ADR-"):
                    continue  # ADR↔ADR 만 cross-check (ticket 등 비-ADR 대상 제외).
                tfm = adrs.get(tgt)
                if tfm is None:
                    findings.append((adr_id, "adr-lifecycle",
                                     f"{verb}: {tgt} 인데 그 ADR 파일이 없음"))
                    continue
                if adr_id not in _as_id_list(tfm.get(back_field)):
                    findings.append((tgt, "adr-lifecycle",
                                     f"{adr_id} 이 {verb} 하는데 {back_field} 에 {adr_id} 누락"))
                tgt_status = str(tfm.get("status") or "").strip()
                # supersession은 amendment보다 강한 종결 상태다. 이미 superseded 된 ADR에
                # 후속 ADR이 amendment 역사를 더해도 상태를 amended로 되돌리지 않는다.
                status_satisfies = (
                    tgt_status == want_status
                    or (verb == "amends" and tgt_status == "superseded")
                )
                if not status_satisfies:
                    findings.append((tgt, "adr-lifecycle",
                                     f"{adr_id} 이 {verb} 하는데 status={tgt_status or '없음'} (기대 {want_status})"))
        # 자가일관 — status 가 amended/superseded 면 back-ref 가 있어야.
        if status == "amended" and not _as_id_list(fm.get("amended_by")):
            findings.append((adr_id, "adr-lifecycle", "status: amended 인데 amended_by 없음"))
        if status == "superseded" and not _as_id_list(fm.get("superseded_by")):
            findings.append((adr_id, "adr-lifecycle", "status: superseded 인데 superseded_by 없음"))
    return findings


def _parse_adr_author(val) -> tuple[str, str] | None:
    """ADR frontmatter `author` 를 `(user, slot)` 으로 파싱한다.

    형식 = `<user>/<pm-slot>` — `created_by`/`claimed_by` identity 토큰과 동일 형태(`identity_tag`).
    *마지막* `/` 로 분리(`rsplit('/', 1)`)해 slot 을 마지막 토큰으로 잡는다 — user 에 `/` 가
    있어도(이메일 등엔 없지만 방어) slot 이 흔들리지 않는다. 두 토큰이 모두 non-empty 여야
    유효(`<user>/<pm-slot>`); `/` 없음·한쪽 빈값(`/slot`·`user/`)은 None(형식 어긋남).
    빈값/None 은 None(부재) — 호출측이 부재와 형식 어긋남을 구분한다.
    """
    s = str(val or "").strip()
    if "/" not in s:
        return None
    user, slot = s.rsplit("/", 1)
    user, slot = user.strip(), slot.strip()
    return (user, slot) if user and slot else None


def lint_adr_author(home) -> list[tuple[str, str, str]]:
    """ADR `author` provenance 권고 advisory.

    각 ADR frontmatter 에 `author: <user>/<pm-slot>`(누가 결정했나·provenance·연속성 아님)가
    박혀 있는지 권고한다 — board.py 가 ADR 을 *발행*하지 않으므로 발행측 규칙을 강제하는 대신
    부재/형식어긋남을 visibility 로만 표면화한다. `author` 부재 → "author 권고"; 있으나
    `<user>/<pm-slot>` 형식이 아니면 → 형식 권고. kind=`adr-author`(`_ADVISORY_LINT_KINDS`
    등재로 `--gate` 종료코드 비기여). decisions/ 부재·깨진 frontmatter → graceful skip
    (신규 clone·구 ADR author 부재 정상 무영향)."""
    findings: list[tuple[str, str, str]] = []
    if not pm_paths.decisions_dir(home).is_dir():
        return findings
    for p in sorted(pm_paths.decisions_dir(home).glob("[0-9]*.md")):
        try:
            fm, _ = ticket_store.load_ticket(p)
        except ticket_store._load_ticket_soft_errors():  # 깨진/frontmatter 없는 파일은 skip(비차단).
            continue
        fm = fm or {}
        adr_id = _adr_id_from_path(p)
        raw = fm.get("author")
        if not str(raw or "").strip():
            findings.append((adr_id, "adr-author",
                             "author 권고 — `author: <user>/<pm-slot>` (누가 결정했나·provenance)"))
        elif _parse_adr_author(raw) is None:
            findings.append((adr_id, "adr-author",
                             f"author 형식 권고 — `{raw}` 이 `<user>/<pm-slot>` 아님"))
    return findings
