"""ADR 발행/개정 공개 CLI — 채번·scaffold·lifecycle back-ref·README 색인·log 배선 (층: cli).

결정 디렉터리·log 경로는 넘겨받은 Home 의 PM 홈 wiki 에서 해소한다.
"""
from __future__ import annotations

import argparse
import re
import sys

from pm_engine.app import adr_contradiction, adr_issue
from pm_engine.domain import adr_rules
from pm_engine.infra import lease_store, pm_paths


# ── contradiction lint 트리거 ───────────────────────────────
# 결정을 개정(amends/supersedes)하는 바로 이 명령이 모순 lint 의 배선점이다 — 재정의 순간
# (인지 시점)에 옛 결정을 참조하는 문서의 잔여 모순을 표면화한다. **개정에만** 발화한다(신규 plain 발행·
# refines 는 참조 스코프가 없거나 대상 불변이라 잔여 모순을 안 만든다). 탐지=LLM(기본 dry·미호출)·판정=
# 사람(advisory·차단 아님). lint 실행 실패는 숨기지 않고 전파한다(ADR 파일은 이미 쓰였다).
def emit_contradiction_advisory(
    home, *, new_number: int, title: str, adr_text: str, amends: list[int], supersedes: list[int],
) -> None:
    """개정(amends/supersedes) 시 모순 lint advisory 를 stderr 로 표면화한다(인지 시점).

    개정 대상이 없으면 no-op. 모순 lint 실행 실패는 전파한다. LLM 은 호출하지 않는다(dry·비용 0)
    — 스코프+안내만 표면화한다."""
    targets = [adr_rules.adr_id(n) for n in (amends + supersedes)]
    if not targets:
        return
    new_id = adr_rules.adr_id(new_number)
    result = adr_contradiction.ContradictionLinter(home).lint(  # dry (run_fn=None·LLM 미호출)
        new_adr_id=new_id, new_adr_title=title, new_adr_text=adr_text, target_ids=targets,
    )
    print("\n" + adr_contradiction.format_advisory(new_id, targets, result), file=sys.stderr)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _resolve_default_author(home) -> "str | None":
    """실행 패키지와 현재 Home으로 기본 작성자를 해소한다. 해소 실패는 전파한다."""
    return lease_store.identity_tag(home)


def _parse_id_list(tokens: list[str]) -> list[int]:
    """`--amends ADR-NNNN --amends 62` 또는 콤마 묶음(`--related a,b`)을 정수/문자열로 파싱."""
    out: list[int] = []
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok):
            if part.strip():
                out.append(adr_rules.parse_adr_num(part))
    return out


def _parse_str_list(tokens: list[str]) -> list[str]:
    """콤마/공백 분리 문자열 리스트 (related·tags 자유 문자열)."""
    out: list[str] = []
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok):
            if part.strip():
                out.append(part.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pm_adr.py",
        description="ADR 발행/개정 명령어化 (채번·scaffold·lifecycle back-ref·README 색인·log).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="새 ADR 발행 (개정 시 --amends/--supersedes/--refines)")
    new.add_argument("--title", required=True, help="ADR 제목")
    new.add_argument("--slug", required=True, help="파일명 slug(영문·hyphen) — NNNN-<slug>.md")
    new.add_argument("--scope", default="internal-process",
                     choices=["internal-process", "mission"], help="결정 scope (기본 internal-process)")
    new.add_argument("--author", default="", help="provenance `<user>/<pm-slot>`")
    new.add_argument("--status", default="accepted",
                     choices=["proposed", "accepted"], help="발행 status (기본 accepted)")
    new.add_argument("--amends", action="append", default=[], metavar="ADR-NNNN",
                     help="개정(부분 수정) 대상 — 대상 status→amended·amended_by 기록 (반복 가능)")
    new.add_argument("--supersedes", action="append", default=[], metavar="ADR-NNNN",
                     help="대체 대상 — 대상 status→superseded·superseded_by 기록 (반복 가능)")
    new.add_argument("--refines", action="append", default=[], metavar="ADR-NNNN",
                     help="확장(대상 불변) 대상 — related 링크만 (반복 가능)")
    new.add_argument("--related", action="append", default=[], metavar="ID",
                     help="관련 링크(ADR/spike/ticket) — 콤마/반복 (개정 대상은 자동 편입)")
    new.add_argument("--tags", action="append", default=[], metavar="TAG",
                     help="frontmatter tags — 콤마/반복")
    new.add_argument("--dry-run", action="store_true", help="쓰기 없이 미리보기만")
    return p


def cmd_new(home, args: argparse.Namespace) -> int:
    # slug 는 파일명이 되므로 부작용(채번·파일 쓰기) 이전에 CLI 입력 단계에서 거부한다(fail-loud·
    # 파일 주입/traversal 방지·validator 클래스 동형).
    try:
        adr_rules.validate_slug(args.slug)
    except ValueError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 2

    # 개정대상 ID 도 부작용 이전 입구에서 rc 2 한 줄 오류로 거부한다
    # ValueError 가 traceback 으로 노출·slug 게이트와 동형 패턴).
    try:
        amends = _parse_id_list(args.amends)
        supersedes = _parse_id_list(args.supersedes)
        refines = _parse_id_list(args.refines)
    except ValueError as exc:
        print(f"[중단] 개정대상 ID 형식 오류 — {exc} (`ADR-NNNN` 또는 숫자만 허용)", file=sys.stderr)
        return 2
    related = _parse_str_list(args.related)
    tags = _parse_str_list(args.tags)

    # --author 생략 시 기존 identity 해소 체인 재사용(board.identity_tag —
    # local.conf identity.user → git config user.email → 세션 토큰). 명시 인자 우선·해소 불가(None)면
    # 현행(빈 값 → lint_adr_author advisory) 유지 — 새 해소 로직 신설 0.
    author = args.author or _resolve_default_author(home) or ""

    issuer = adr_issue.AdrIssuer(
        decisions_dir=pm_paths.decisions_dir(home),
        log_file=pm_paths.wiki_dir(home) / "log" / "current.md",
    )
    plan = issuer.plan(
        title=args.title, slug=args.slug, scope=args.scope, author=author,
        status=args.status, amends=amends, supersedes=supersedes, refines=refines,
        related=related, tags=tags,
    )
    number = plan["number"]

    if args.dry_run:
        print(f"[dry-run] 발행 예정: {adr_rules.adr_id(number)} → {plan['adr_path']}")
        print("\n── 신규 ADR 파일 ──")
        print(plan["adr_text"])
        for tpath, _ in plan["target_edits"]:
            print(f"── back-ref 기록: {tpath.name} ──")
        print("\n── log decide entry ──")
        print(plan["log_entry"])
    else:
        issuer.apply(plan)
        print(f"✓ {adr_rules.adr_id(number)} 발행 → {plan['adr_path'].name}")
        for tpath, _ in plan["target_edits"]:
            print(f"  ✓ back-ref 기록: {tpath.name}")
        if plan["readme_text"] is not None:
            print("  ✓ README 색인 갱신")
        print("  ✓ log/current.md decide entry append")

    for w in plan["warnings"]:
        print(f"  ⚠ {w}", file=sys.stderr)

    # 모순 lint 트리거 — 개정(amends/supersedes)에만 발화. dry-run/apply 공통으로
    # 인지 시점(재정의 명령)에 잔여 모순 스코프를 표면화한다(advisory·fail-soft·차단 아님).
    emit_contradiction_advisory(
        home, new_number=number, title=args.title, adr_text=plan["adr_text"],
        amends=amends, supersedes=supersedes,
    )

    print("\n남은 PM 손작업: ADR 본문 서술(Context/Decision/Consequences/References) + "
          "README 개정 요약 cell + log decide 본문 + git commit.")
    return 0


def main(home, argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "new":
        return cmd_new(home, args)
    parser.error(f"unknown command: {args.cmd}")
    return 2
