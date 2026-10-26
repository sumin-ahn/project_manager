"""domain 레이어 공개 CLI — list·affected·capture·capture-draft·coverage·lint 배선 (층: cli).

domain 페이지·engine.manifest 경로는 넘겨받은 Home 의 PM 홈에서 해소한다.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from pm_engine.app import doc_affected, doc_coverage, doc_lint, doc_staleness
from pm_engine.domain import doc_drafts
from pm_engine.infra import doc_page_store, pm_paths, repo_coordinates, ticket_store


# capture에서 디렉토리 touch 하나가 표시하는 gap 상한. 전체 gap API는 자르지 않고,
# CLI 기본 출력만 접는다(`capture --all-gaps`로 전체 표시).
DIRECTORY_GAP_DISPLAY_LIMIT = 15


_WORKTREE_TOUCH_PREFIX = re.compile(r"^work/[^/]+_\d+(?:/|$)")


def _has_worktree_touch_prefix(path: str) -> bool:
    """touch 가 `work/<repo>_<N>/` 슬롯 접두를 가졌는지 표기 변형에 견고하게 보는 경량 판정."""
    norm = path.replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return _WORKTREE_TOUCH_PREFIX.match(norm) is not None


def _normalize_ticket_touches(home, touches: list[str]) -> list[str]:
    """ticket PM-home 좌표를 경로별 advisory 경고와 함께 정규화한다.

    접두가 하나도 없으면 좌표 정규화를 건너뛰고 입력을 그대로 돌려준다.
    조회 표면은 한 경로 오류가 배치를 죽이지 않는다. 오류 경로는 loud
    경고 후 제외하고 나머지를 계속 처리한다(검증 없이 strip하거나 원문을 하류에 넘기지 않음).
    """
    if not any(_has_worktree_touch_prefix(path) for path in touches):
        return touches
    normalized: list[str] = []
    for path in touches:
        if not _has_worktree_touch_prefix(path):
            normalized.append(path)
            continue
        try:
            normalized.append(repo_coordinates.normalize_repo_path(path, pm_root=home.root))
        except repo_coordinates.RepoCoordinateError as exc:
            print(
                f"domain: touch {path!r} 좌표 정규화 경고 — {exc}; 이 경로는 제외",
                file=sys.stderr,
            )
    return normalized


# ── CLI ───────────────────────────────────────────────────────────────────────


def _stale_marker(home, page: dict) -> str:
    """페이지 줄 앞 stale 마커 — stale(True)=`⚠ `·None(unknown)/False=무표시(공백 정렬).

    list/affected 가 공유한다(DRY). page_stale==True 만 ⚠ — unknown(git 부재 등)은 조용히
    무표시(노이즈 방지). 마커 폭(2칸)을 비-stale 줄에도 채워 줄을 정렬한다.
    """
    return "⚠ " if doc_staleness.page_stale(home, page) is True else "  "


def cmd_list(home, args: argparse.Namespace) -> int:
    pages = doc_page_store.load_pages(pm_paths.domain_dir(home))
    if not pages:
        print("(domain 페이지 없음)")
        return 0
    for page in pages:
        marker = _stale_marker(home, page)
        title = page["title"]
        ptype = page["type"]
        covers = ",".join(page["covers"])
        updated = page["updated"] or ""
        print(f"{marker}{title}  ·  {ptype}  ·  {covers}  ·  {updated}")
    return 0


def _touches_from_ticket(home, ticket_id: str) -> list[str]:
    """ticket_store.load_ticket 으로 ticket frontmatter 의 `touches` 를 읽는다(없으면 []).

    ticket 부재/깨짐 → [](graceful·crash 0). frontmatter `touches` 의
    문자열 원소만 취한다(비-문자열 오기는 방어적으로 버림 — parse_page covers 동형).
    """
    try:
        _status, path = ticket_store.find_ticket(home, ticket_id)
        fm, _body = ticket_store.load_ticket(path)
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


def cmd_affected(home, args: argparse.Namespace) -> int:
    """ticket touches(또는 --touches) ∩ domain covers 로 영향받는 페이지를 출력한다."""
    if args.ticket:
        touches = _touches_from_ticket(home, args.ticket)
        try:
            # --ticket touches만 PM-home 좌표 계약을 가진다. 사용자가 직접 넘긴 --touches는
            # 이미 이 CLI의 repo 상대 입력 계약이므로 변환하지 않는다.
            touches = _normalize_ticket_touches(home, touches)
        except RuntimeError as exc:
            print(
                f"domain: ticket {args.ticket} touches 좌표 정규화 실패 — {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        # --touches a,b,c — 콤마분리·공백 trim·빈 토큰 제거.
        touches = [t.strip() for t in args.touches.split(",") if t.strip()]
    pages = doc_affected.pages_for_touches(
        home, touches, doc_page_store.load_pages(pm_paths.domain_dir(home)))
    if not pages:
        print("(영향 domain 페이지 없음)")
        return 0
    for page in pages:
        marker = _stale_marker(home, page)
        title = page["title"]
        covers = ",".join(page["covers"])
        print(f"{marker}{title}  ·  {covers}")
    return 0


def _touches_from_tickets(home, tickets_csv: str) -> list[str]:
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
        for touch in _touches_from_ticket(home, tid):
            if touch in seen:
                continue
            seen.add(touch)
            out.append(touch)
    return out


@doc_affected._repository_query_batch()
def cmd_capture(home, args: argparse.Namespace) -> int:
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
        touches = _touches_from_tickets(home, ",".join(args.tickets))
        try:
            touches = _normalize_ticket_touches(home, touches)
        except RuntimeError as exc:
            print(
                f"domain: tickets touches 좌표 정규화 실패 — {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        # --touches a,b,c — affected 동형(콤마분리·공백 trim·빈 토큰 제거).
        touches = [t.strip() for t in args.touches.split(",") if t.strip()]

    pages = doc_page_store.load_pages(pm_paths.domain_dir(home))
    affected = doc_affected.pages_for_touches(home, touches, pages)
    # pages_for_touches가 owner-mismatch advisory를 이미 냈다. gap 계산은 같은 touch를 다시
    # 보므로 여기서는 중복 경고만 끄고 owner 필터/coverage 판정은 그대로 쓴다.
    gap_groups = doc_affected._uncovered_path_groups(
        home,
        touches, pages, warn_owner_mismatch=False)
    gaps = [
        path
        for _touch, group_paths in gap_groups
        for path in group_paths
    ]

    if not affected and not gaps:
        # 파일 단위 recall은 선두 wildcard covers가 디렉토리 prefix 규칙에서 빠지던 원인을
        # 없앴다. 이 가드는 그 원인 하나가 아니라 판정 대상이 양쪽에서 사라지는 증상 전체를
        # 계속 loud하게 드러낸다.
        judgment_targets: list[str] = []
        expanded_file_count = 0
        expanded_directory_touch = False
        for touch in touches:
            if not isinstance(touch, str) or not touch.strip():
                continue
            norm = touch if touch.strip() == touch else touch.strip()
            is_directory_touch = (
                doc_affected._directory_touch_location(home, norm) is not None)
            expanded = doc_affected._files_for_directory_touch(home, norm)
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
        print(
            "  ※ 갱신 = 현재-진실을 *교체*(지금 유효한 서술로 고쳐 쓰기). "
            "세션별 delta 를 덧붙이지 마라 — '언제 왜 바뀌었나'는 log/ADR 몫이고 "
            "페이지는 링크만 건다(domain lint `history` 축)."
        )
        for page in affected:
            marker = _stale_marker(home, page)
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


def cmd_capture_draft(home, args: argparse.Namespace) -> int:
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
        path = doc_page_store.write_draft_page(
            home,
            args.title,
            ptype=args.type,
            covers=covers,
            slug=args.slug,
            source=args.source,
            domain_dir=pm_paths.domain_dir(home),
        )
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError 는 OSError 하위 아님 — 비-UTF8 source 파일을 명시 에러로(traceback 방지).
        print(f"domain: capture-draft 실패 — source 읽기/쓰기 오류: {exc}", file=sys.stderr)
        return 1
    print(f"draft 생성: {path}")
    print("  status: draft (index 제외 — affected/lint/recall 안 보임).")
    print("  promote(정식화·사람 손): frontmatter status:draft 제거 + 파일명 .draft.md→.md rename + git add.")
    return 0


def cmd_coverage(home, _args: argparse.Namespace) -> int:
    """출하 엔진 도구의 domain coverage를 기계 집계해 출력한다(advisory·항상 exit 0)."""
    pages = doc_page_store.load_pages(pm_paths.domain_dir(home))
    report = doc_coverage.coverage_report(
        home,
        pages,
        domain_dir=pm_paths.domain_dir(home),
        manifest_path=pm_paths.engine_manifest(home),
        exemptions=doc_coverage.DOMAIN_COVERAGE_EXEMPTIONS,
    )
    if report["status"] in {"skipped", "error"}:
        print(f"domain coverage: {report['status']} — {report['reason']}")
        return 0

    print("domain coverage (denominator: engine.manifest shipped Python tools)")
    readme_body, _readme_error = doc_coverage._coverage_readme_text(pm_paths.domain_dir(home))
    if not (
        readme_body and doc_coverage.COVERAGE_CLAIM_INTENT_RE.search(readme_body)
    ):
        print("  claim: none (이 트리는 coverage 미주장; 아래 집계는 진단 전용)")
    print(f"  tools: {len(report['tools'])}")
    print(f"  covered: {len(report['covered'])}")
    print(f"  exempt: {len(report['exemptions'])}")
    print(f"  gaps: {len(report['gaps'])}")
    print(f"  status: {report['status']}")
    if report["exemptions"]:
        print("  exemptions:")
        for path, reason in report["exemptions"].items():
            print(f"    {path} — {reason}")
    if report["gaps"]:
        print("  coverage gaps:")
        for path in report["gaps"]:
            print(f"    {path}")
    if report["exemption_errors"]:
        print("  invalid exemptions:")
        for detail in report["exemption_errors"]:
            print(f"    {detail}")
    return 0


def cmd_lint(home, args: argparse.Namespace) -> int:
    """domain lint — freshness+coverage finding 출력 (advisory·항상 exit 0).

    finding 1줄 = `kind · page · detail`. clean 이면 freshness·coverage 경계를 함께 알린다. *비차단* —
    어느 경우도 rc 0(visibility·작업/완료 막지 않음).
    """
    pages = doc_page_store.load_pages(pm_paths.domain_dir(home))
    # `doc_lint.OVERSIZED_LINES` 를 호출 시점에 읽어 명시 전달 — lint_pages 의 기본 인자는 정의
    # 시점에 굳어 monkeypatch(테스트)·재바인딩을 못 본다.
    findings = doc_lint.lint_pages(
        home, pages, oversized_lines=doc_lint.OVERSIZED_LINES,
        domain_dir=pm_paths.domain_dir(home), manifest_path=pm_paths.engine_manifest(home),
    )
    if not findings:
        print("✓ domain freshness 양호 (coverage 포함·미주장 시 검사 제외)")
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
        help="이 ticket 의 frontmatter touches 로 영향 페이지를 찾는다 (ticket_store.load_ticket).",
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
        "--type", default=doc_drafts.DEFAULT_DRAFT_TYPE, choices=["concept", "guide", "research"],
        help=f"페이지 type (기본 {doc_drafts.DEFAULT_DRAFT_TYPE}).",
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

    p_coverage = sub.add_parser(
        "coverage",
        help="engine.manifest 출하 도구의 domain covers·면제·gap 기계 집계 (read-only·exit 0)",
    )
    p_coverage.set_defaults(fn=cmd_coverage)

    p_lint = sub.add_parser(
        "lint",
        help="domain lint — freshness+coverage finding (advisory·exit 0)",
    )
    p_lint.set_defaults(fn=cmd_lint)

    return parser


def main(home, argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(home, args)
