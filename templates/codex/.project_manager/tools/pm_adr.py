#!/usr/bin/env python3
"""ADR 발행/개정 명령어化 backbone — 흩어진 손 단계(채번·frontmatter·lifecycle back-ref·
README 색인·log decide entry)를 한 명령으로 원자화한다.

사용:
    python3 .project_manager/tools/pm_adr.py new \\
      --title "제목" --slug short-slug --scope internal-process \\
      --author "user/pm-slot" \\
      [--amends ADR-0061 --amends ADR-0062] [--supersedes ADR-0043] [--refines ADR-0033] \\
      [--related ADR-0019,ADR-0050] [--tags board,ticket-id] \\
      [--status accepted] [--dry-run]

동작 (하나라도 실패하면 이후 단계 미착수·apply 는 마지막에 한 번에):
  1. 채번 — decisions/ 스캔·다음 NNNN.
  2. 신규 ADR 파일 scaffold — `decisions/NNNN-slug.md`(frontmatter + 본문 골격).
  3. lifecycle back-ref 기록 — `--amends`/`--supersedes` 대상 ADR frontmatter 에 status(amended/
     superseded) + amended_by/superseded_by 기록(발행 시점 충족·사후 lint 아님).
  4. README 색인 — Accepted 표에 신규 행 추가 + amends/supersedes 대상 행 Accepted→Amended/
     Superseded 표 이동(또는 이미 이동됐으면 back-ref cell 에 append).
  5. log/current.md decide entry skeleton append.

설계:
  - 경로 해소는 **인스턴스-상대**(self-location·`Path(__file__).parents[2]`) — ADR 실경로는
    채택자 `.project_manager/wiki/decisions/`. 엔진은 어느 인스턴스에서 돌든 자기 위치에서 해소한다.
  - 신규 파일 frontmatter 는 flow-list 문자열 템플릿으로 쓴다(기존 ADR 스타일 보존·yaml round-trip
    reformat 회피). 대상 ADR 개정은 **surgical 정규식 치환**(frontmatter 블록만·본문·포맷 불변).
  - 판단 요소(개정 요약·본문 서술)는 placeholder(`<... PM 서술>`) — 기계는 파생 가능한 것만
    채운다(pm_handoff/ticket_finish skeleton 철학 계승).
  - DI seam — decisions_dir/readme_file/log_file/date 를 주입해 hermetic 테스트(실 wiki 미접촉).
  - README 조작은 fail-soft — 구조(표) 불일치 시 crash 아니라 warning + 해당 단계 skip
    (frontmatter back-ref = load-bearing 은 항상 수행·lint 정합 보장).
  - 구현은 `pm_engine.domain.adr_rules`(순수 규칙)·`pm_engine.app.adr_issue`(채번·발행)이고 이 파일은
    CLI 진입이다.
  - LLM 미호출 — stdlib + PyYAML(대상 frontmatter 파싱)만.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.infra import console, pm_paths, repo_files  # noqa: E402
from pm_engine.app import adr_contradiction, adr_issue  # noqa: E402
from pm_engine.domain import adr_rules  # noqa: E402
_load_module_from_path = repo_files.load_module

REPO = Path(__file__).resolve().parents[2]
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"
README_FILE = DECISIONS_DIR / "README.md"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.8.1"


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
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지 (fail-soft 재-raise 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


# ── contradiction lint 트리거 ───────────────────────────────
# 결정을 개정(amends/supersedes)하는 바로 이 명령이 모순 lint 의 배선점이다 — 재정의 순간
# (인지 시점)에 옛 결정을 참조하는 문서의 잔여 모순을 표면화한다. **개정에만** 발화한다(신규 plain 발행·
# refines 는 참조 스코프가 없거나 대상 불변이라 잔여 모순을 안 만든다). 탐지=LLM(기본 dry·미호출)·판정=
# 사람(advisory·차단 아님) — 이 트리거는 어떤 경우에도 발행을 막지 않는다(fail-soft·감싸 호출).
def emit_contradiction_advisory(
    *, new_number: int, title: str, adr_text: str, amends: list[int], supersedes: list[int],
) -> None:
    """개정(amends/supersedes) 시 모순 lint advisory 를 stderr 로 표면화한다(인지 시점).

    개정 대상이 없으면 no-op. 모순 lint 오류는 조용히 무시(fail-soft — 부가
    advisory 가 발행을 막지 않는다). LLM 은 호출하지 않는다(dry·비용 0) — 스코프+안내만 표면화한다."""
    targets = [adr_rules.adr_id(n) for n in (amends + supersedes)]
    if not targets:
        return
    try:
        new_id = adr_rules.adr_id(new_number)
        result = adr_contradiction.ContradictionLinter(pm_paths.home_at(REPO)).lint(  # dry (run_fn=None·LLM 미호출)
            new_adr_id=new_id, new_adr_title=title, new_adr_text=adr_text, target_ids=targets,
        )
        print("\n" + adr_contradiction.format_advisory(new_id, targets, result), file=sys.stderr)
    except Exception:
        return  # advisory 는 fail-soft — 어떤 오류도 발행을 막지 않는다.


# ── CLI ──────────────────────────────────────────────────────────────────────

def _resolve_default_author() -> "str | None":
    """`--author` 생략 시 board `identity_tag()` sibling 재사용 — `<user>/<pm-slot>` 해소.

    board.py 의 기존 identity 해소 체인(local.conf identity.user → git config user.email·세션 토큰)을
    그대로 쓴다(자체 로직 신설 0). 로드/해소 실패는 None
    (fail-soft — 호출부가 현행 빈 값 경로 유지·발행을 못 깨게)."""
    try:
        board_path = Path(__file__).resolve().parent / "board.py"
        mod = _load_module_from_path(
            board_path, "board.py", verifier=_verify_engine_rev,
        )
        return mod.identity_tag()
    except Exception as exc:  # noqa: BLE001 — identity 해소 실패는 advisory 경로로(발행 무영향).
        if _is_engine_rev_skew(exc):
            raise  # board 사본 skew 는 fail-loud(삼키지 않는다).
        return None


def _parse_id_list(tokens: list[str]) -> list[int]:
    """`--amends ADR-0061 --amends 62` 또는 콤마 묶음(`--related a,b`)을 정수/문자열로 파싱."""
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


def cmd_new(args: argparse.Namespace) -> int:
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
    author = args.author or _resolve_default_author() or ""

    issuer = adr_issue.AdrIssuer(decisions_dir=DECISIONS_DIR, log_file=LOG_FILE)
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
        new_number=number, title=args.title, adr_text=plan["adr_text"],
        amends=amends, supersedes=supersedes,
    )

    print("\n남은 PM 손작업: ADR 본문 서술(Context/Decision/Consequences/References) + "
          "README 개정 요약 cell + log decide 본문 + git commit.")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "new":
        return cmd_new(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        console.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
