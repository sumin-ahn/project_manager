"""domain 페이지 coverage 판정 (층: app).

출하 엔진 도구 대비 covers 미포함 도구와 면제, README 의 coverage 주장 대조를 판정한다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from pm_engine.domain import doc_pages
from pm_engine.infra import fs, manifest_store
from pm_engine.app import doc_affected


# README 는 derivable 수치를 복제하지 않고 이 커맨드 중 하나를 가리킨다. Windows 1순위
# 런처를 canonical 로 두고 POSIX/venv 표기도 허용한다(확장 상수는 coverage helper 옆).
COVERAGE_COMMAND = "py -3.12 .project_manager/tools/domain.py coverage"
COVERAGE_COMMAND_ALTERNATES = ("python3 .project_manager/tools/domain.py coverage",
                               "python .project_manager/tools/domain.py coverage")
COVERAGE_CLAIM_RE = re.compile(
    r"<!--\s*domain-coverage-claim:\s*(complete|incomplete)\s*-->",
    re.IGNORECASE,
)

# 출하 도구 중 domain 개념으로 채록할 필요가 없는 예외. key=repo 상대 경로, value=사유.
# 빈 사유·manifest 밖 경로·이미 covers 된(stale) 등재는 coverage lint finding 이다.
# "면제 0"은 이 상수를 설명하는 선언이 아니라 coverage 집계 출력의 len(valid exemptions)이다.
DOMAIN_COVERAGE_EXEMPTIONS: dict[str, str] = {}


# ── 출하 엔진 domain coverage (capture gap 판정 재사용) ───────────────────────


def engine_tool_paths(manifest_path: Path) -> list[str]:
    """``engine.manifest``가 출하한다고 선언한 ``tools/*.py`` repo 상대 경로.

    커버리지 분모는 디렉토리에 우연히 존재하는 파일이 아니라 업데이트로 채택자에게 **출하되는
    엔진**이다. 그 단일 진실이 ``engine.manifest``라 manifest 등재분을 택한다. 경로 비교는
    ``Path.parts``/``parent``로 하며 문자열 구분자나 대소문자 정규화를 가정하지 않는다.
    반환 문자열만 기존 covers/capture 좌표계(repo 상대 POSIX)에 맞춘다.
    """
    return _engine_tool_paths_from_manifest(Path(manifest_path))


def _coverage_skip_reason(domain_dir: Path, pages: list[dict]) -> str | None:
    """채택자 wiki의 세 가지 page-0 형상을 구분해 조용하지 않은 skip 사유를 돌려준다."""
    domain_dir = Path(domain_dir)
    if not domain_dir.is_dir():
        return f"{domain_dir} 디렉토리 없음"
    if pages:
        return None
    try:
        entries = list(domain_dir.iterdir())
    except OSError as exc:
        return f"domain/ 열거 실패 ({exc})"
    if not entries:
        return "정식 domain 페이지 0장 (domain/ 디렉토리 비어 있음)"
    if all(p.is_file() and p.name in doc_pages._NON_PAGE_FILES for p in entries) and any(
            p.name == "_template.md" for p in entries):
        return "정식 domain 페이지 0장 (_template.md만 존재)"
    return "정식 domain 페이지 0장 (파싱 실패·초안·비페이지 제외 후)"


def coverage_report(
        home,
        pages: list[dict],
        *,
        domain_dir: Path,
        manifest_path: Path,
        exemptions: dict[str, str] | None = None,
) -> dict:
    """출하 도구 coverage 집계 결과.

    raw gap 판정은 새로 구현하지 않고 ``capture --all-gaps``의 단일 진실
    ``uncovered_paths``를 그대로 소비한다. 면제는 raw gap과 manifest 분모 양쪽에 속하고
    비어 있지 않은 사유를 가져야만 유효하다. 유효하지 않은 등재는 숨김에 쓰지 않는다.
    """
    domain_dir = Path(domain_dir)
    skip_reason = _coverage_skip_reason(domain_dir, pages)
    if skip_reason is not None:
        return _coverage_unavailable_report(domain_dir, skip_reason)

    source_error = _coverage_source_report(domain_dir, Path(manifest_path), pages)
    if source_error is not None:
        return source_error

    try:
        tools = engine_tool_paths(Path(manifest_path))
    except (OSError, UnicodeError, RuntimeError) as exc:
        return {
            "status": "error",
            "reason": f"engine.manifest 읽기 실패: {exc}",
            "tools": [],
            "covered": [],
            "exemptions": {},
            "gaps": [],
            "exemption_errors": [],
        }
    if not tools:
        return _coverage_empty_denominator_report()

    raw_gaps = list(doc_affected.uncovered_paths(
        home,
        tools,
        pages,
        warn_owner_mismatch=False,
    ))
    raw_gap_set = {str(path) for path in raw_gaps}
    tool_set = set(tools)
    declared = DOMAIN_COVERAGE_EXEMPTIONS if exemptions is None else exemptions
    valid_exemptions: dict[str, str] = {}
    exemption_errors: list[str] = []
    for raw_path, raw_reason in declared.items():
        path = Path(str(raw_path)).as_posix()
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if path not in tool_set:
            exemption_errors.append(f"{path}: engine.manifest 출하 도구가 아님")
        elif not reason:
            exemption_errors.append(f"{path}: 면제 사유가 비어 있음")
        elif path not in raw_gap_set:
            exemption_errors.append(f"{path}: 이미 covers 됨 (stale 면제)")
        else:
            valid_exemptions[path] = reason

    gaps = [path for path in raw_gaps if str(path) not in valid_exemptions]
    covered = [path for path in tools if path not in raw_gap_set]
    status = "complete" if not gaps and not exemption_errors else "incomplete"
    return {
        "status": status,
        "reason": None,
        "tools": tools,
        "covered": covered,
        "exemptions": valid_exemptions,
        "gaps": gaps,
        "exemption_errors": exemption_errors,
    }


def coverage_findings(
        home,
        pages: list[dict],
        *,
        domain_dir: Path,
        manifest_path: Path,
        exemptions: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """README qualitative claim ↔ 기계 집계 대조 finding (kind=coverage·advisory).

    README는 derivable 숫자를 적지 않고 ``COVERAGE_COMMAND``와 qualitative marker
    ``domain-coverage-claim: complete|incomplete``를 각각 정확히 하나 둔다. 미주장 page-0
    채택자는 stderr 사유와 함께 skip하되, claim 활성 트리의 모든 unavailable/skip 경로는
    finding으로 닫는다.
    """
    domain_dir = Path(domain_dir)
    report = coverage_report(
        home,
        pages,
        domain_dir=domain_dir,
        manifest_path=manifest_path,
        exemptions=exemptions,
    )
    if report["status"] == "skipped":
        body, _readme_error = _coverage_readme_text(domain_dir)
        if body is not None and COVERAGE_CLAIM_INTENT_RE.search(body):
            return [(
                "coverage", "domain coverage",
                f"claim 활성인데 coverage 집계 불가 — {report['reason']}",
            )]
        print(f"domain: coverage 검사 skip — {report['reason']}", file=sys.stderr)
        return []
    if report["status"] == "error":
        return [("coverage", "domain coverage", report["reason"])]

    findings: list[tuple[str, str, str]] = []
    for detail in report["exemption_errors"]:
        findings.append(("coverage", "면제 등재", detail))
    for path in report["gaps"]:
        findings.append((
            "coverage",
            Path(str(path)).name,
            f"{path}: 담당 covers 또는 유효한 사유 면제 없음",
        ))

    body, readme_error = _coverage_readme_text(domain_dir)
    if body is None and readme_error == "README.md 없음":
        findings.append(("coverage", "README.md", "커버리지 README 없음"))
        return findings
    if body is None:
        findings.append(("coverage", "README.md", readme_error or "읽기 실패"))
        return findings

    command_refs = tuple(
        f"`{command}`"
        for command in (COVERAGE_COMMAND, *COVERAGE_COMMAND_ALTERNATES)
    )
    command_counts = tuple(body.count(ref) for ref in command_refs)
    if not any(command_counts) or any(count > 1 for count in command_counts):
        findings.append((
            "coverage",
            "README.md",
            "허용 집계 커맨드를 inline-code로 하나 이상 가리키되 같은 표기를 반복하지 말 것: "
            + " 또는 ".join(command_refs),
        ))

    # 유효값만 세면 ``complete`` 1개에 오타 marker를 덧붙여도 통과한다. claim을
    # 쓰려 한 모든 흔적을 먼저 세고, 그 단 하나가 유효값일 때만 주장으로 채택한다.
    claim_intents = COVERAGE_CLAIM_INTENT_RE.findall(body)
    claims = COVERAGE_CLAIM_RE.findall(body)
    if len(claim_intents) != 1 or len(claims) != 1:
        findings.append((
            "coverage",
            "README.md",
            "claim marker를 정확히 1개 둬야 함: "
            "<!-- domain-coverage-claim: complete|incomplete -->",
        ))
    else:
        claimed = claims[0].lower()
        actual = report["status"]
        if claimed != actual:
            findings.append((
                "coverage",
                "README.md",
                f"claim={claimed}, 실측={actual} — coverage 커맨드 결과와 불일치",
            ))

    numeric_finding = _numeric_coverage_prose_finding(body)
    if numeric_finding is not None:
        findings.append(numeric_finding)
    return findings


# 값 오타도 "claim 의도"로 보아 lint를 켠다. marker 자체가 전혀 없는 README만 opt-out이다.
COVERAGE_CLAIM_INTENT_RE = re.compile(
    r"<!--\s*domain-coverage-claim\s*:",
    re.IGNORECASE,
)

# README의 사람이 읽는 산문에 파생 수치를 다시 복제하는 좁은 패턴만 막는다. 일반 숫자·날짜나
# "도구 수는 커맨드로 확인" 같은 정성 문장은 허용하고, 원 결함 형태인 엔진 도구 개수·면제 0·
# coverage 문맥의 합계 등식만 잡아 오탐 범위를 한정한다. 표(`| 면제 | 0 |`)는 셀 구조 해석,
# 한글 수사(`열일곱`)는 형태소/수사 정규화가 필요하고 문장 분할은 문맥 추론이 필요하므로 이
# 좁은 regex lint의 의도적 경계다.
NUMERIC_COVERAGE_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"엔진\s+(?:\d+\s*(?:개\s*)?도구|도구\s*\d+\s*개)",
        re.IGNORECASE,
    ),
    re.compile(
        r"면제(?:\s*\([^)\n]{1,24}\)|\s+[A-Za-z가-힣][A-Za-z가-힣_-]{0,23})?"
        r"\s*[는은]?\s*[=:]?\s*0(?!\d|\.\d|\s*(?:원|%))(?:\s*(?:개|건))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)\d+\s*=\s*\d+\s+(?:concept|guide|research)"
        r"(?:\s*/\s*(?:concept|guide|research))*"
        r"\s*\+\s*\d+\s+(?:concept|guide|research)(?![A-Za-z])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:엔진|도구|면제|gap|갭|coverage|커버리지)"
        r"[^.!?\n]{0,80}(?<!\d)\d+\s*=\s*\d+\s*\+\s*\d+(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\d)\d+\s*=\s*\d+\s*\+\s*\d+(?!\d)"
        r"[^.!?\n]{0,80}(?:엔진|도구|면제|gap|갭|coverage|커버리지)",
        re.IGNORECASE,
    ),
)


def _engine_tool_paths_from_manifest(manifest_path: Path) -> list[str]:
    """실제 출하 inventory를 tools/ 직계 Python 도구 분모로 협착한다."""
    tools_parent = Path(".project_manager") / "tools"
    repo_root = manifest_path.parent.parent
    paths: list[str] = []
    seen: set[Path] = set()

    def add_tool(rel: Path) -> None:
        # 분모는 tools/ 바로 아래 Python CLI만이다. 디렉토리 엔트리는 재귀 출하되더라도
        # nested helper를 독립 엔진 도구로 세지 않는다.
        if rel.parent != tools_parent or rel.suffix != ".py" or rel in seen:
            return
        seen.add(rel)
        paths.append(rel.as_posix())

    # parser와 파일/디렉토리 전개를 모두 update의 canonical 경로에서 가져온다. 여기서는
    # coverage 제품 정의인 tools/ 직계 Python CLI 협착만 소유한다. flavor manifest의
    # @source는 upstream에만 있는 어댑터 source를 정상적으로 가리키므로, destination이 이
    # 분모에 기여할 수 없는 항목은 source 존재 여부까지 검사하지 않는다.
    manifest = manifest_store.read_manifest(manifest_path)
    for entry_index, entry in enumerate(manifest):
        rel = Path(str(entry))
        if rel.is_absolute() or ".." in rel.parts:
            continue
        can_ship_tools_tree = rel == tools_parent or tools_parent.is_relative_to(rel)
        can_ship_direct_python = rel.parent == tools_parent and rel.suffix == ".py"
        if not (can_ship_tools_tree or can_ship_direct_python):
            continue
        files, source_missing, target_owned = manifest_store.manifest_entry_shipping_inventory(
            repo_root,
            manifest,
            entry_index,
            repo_root,
        )
        if source_missing:
            if target_owned:
                continue
            raise RuntimeError(
                "non-@target-owned manifest source가 없어 출하 inventory를 해석할 수 없음: "
                f"{entry}"
            )
        for shipped_rel, _source in files:
            add_tool(Path(shipped_rel))
    return paths


def _coverage_readme_text(domain_dir: Path) -> tuple[str | None, str | None]:
    """README를 읽어 `(fence 제외 본문, 오류)`로 반환한다."""
    readme = Path(domain_dir) / "README.md"
    try:
        return doc_pages._markdown_without_fenced_code(fs.read_text_shared(readme, encoding="utf-8")), None
    except FileNotFoundError:
        return None, "README.md 없음"
    except (OSError, UnicodeError) as exc:
        return None, f"README.md 읽기 실패: {exc}"


def _coverage_source_skip_reason(
        domain_dir: Path,
        manifest_path: Path,
        pages: list[dict],
) -> str | None:
    """pages·domain README·manifest가 한 채택자 트리에 속하는지 검증한다."""
    try:
        domain_resolved = Path(domain_dir).resolve(strict=False)
        manifest_resolved = Path(manifest_path).resolve(strict=False)
        expected_manifest = (
            domain_resolved.parent.parent / "engine.manifest"
        ).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"coverage 트리 경로 해소 실패 ({exc})"
    if manifest_resolved != expected_manifest:
        return (
            "pages/domain/engine.manifest 트리 불일치 "
            f"(domain={domain_resolved}, manifest={manifest_resolved})"
        )
    for page in pages:
        try:
            Path(page["path"]).resolve(strict=False).relative_to(domain_resolved)
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            return f"pages 출처가 domain 트리와 불일치: {page.get('path', '<path 없음>')}"
    return None


def _coverage_source_report(
        domain_dir: Path,
        manifest_path: Path,
        pages: list[dict],
) -> dict | None:
    """트리 불일치를 claim-aware unavailable report로 바꾼다."""
    reason = _coverage_source_skip_reason(domain_dir, manifest_path, pages)
    if reason is None:
        return None
    return _coverage_unavailable_report(domain_dir, reason)


def _coverage_unavailable_report(domain_dir: Path, reason: str) -> dict:
    """집계 불가를 미주장 트리 skip/claim 활성 트리 error로 분기한다.

    coverage의 모든 빈 입력·출처 불일치 경로가 이 단일 생성기를 지나므로, README가 claim
    의도를 가진 순간 ``skipped``가 주장을 공짜로 충족시키지 못한다. coverage_findings도
    예상 밖의 신규 skipped report를 독립적으로 방어한다.
    """
    body, _readme_error = _coverage_readme_text(Path(domain_dir))
    claimed = bool(body is not None and COVERAGE_CLAIM_INTENT_RE.search(body))
    return {
        "status": "error" if claimed else "skipped",
        "reason": (
            f"claim 활성인데 coverage 집계 불가 — {reason}"
            if claimed else reason
        ),
        "tools": [],
        "covered": [],
        "exemptions": {},
        "gaps": [],
        "exemption_errors": [],
    }


def _coverage_empty_denominator_report() -> dict:
    """공허한 참을 막는 분모 0 error report."""
    return {
        "status": "error",
        "reason": (
            "engine.manifest의 출하 Python 도구 분모가 0 — "
            "manifest가 비었거나 tools 엔트리를 해석할 수 없음"
        ),
        "tools": [],
        "covered": [],
        "exemptions": {},
        "gaps": [],
        "exemption_errors": [],
    }


def _numeric_coverage_prose_finding(body: str) -> tuple[str, str, str] | None:
    """사람이 읽는 README 산문의 하드코딩 coverage 수치 finding."""
    prose = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    if not any(pattern.search(prose) for pattern in NUMERIC_COVERAGE_CLAIM_PATTERNS):
        return None
    return (
        "coverage",
        "README.md",
        "수치형 coverage 산문 금지 — 도구·면제·gap 수는 집계 커맨드 결과를 가리킬 것",
    )


def _has_engine_coverage_context(body: str) -> bool:
    """marker 없는 README가 엔진 coverage 문맥인지 좁게 식별한다."""
    prose = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return bool(
        re.search(r"\b엔진\b", prose, re.IGNORECASE)
        and re.search(
            r"(?:도구|면제|gap|갭|coverage|커버리지)",
            prose,
            re.IGNORECASE,
        )
    )
