"""모순 lint 공개 CLI — 개정된 결정을 참조하는 문서의 잔여 모순 후보 표면화 (층: cli).

standalone 재검사 진입이다. ADR 발행 명령(`cli.adr_cmd`)은 `pm_engine.app.adr_contradiction` API 를
직접 부른다.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from pm_engine.app import adr_contradiction
from pm_engine.infra import fs, pm_paths


def _load_adr(decisions_dir: Path, num: int) -> tuple[str, str]:
    """decisions/ 에서 ADR 번호 → (title, 전체 텍스트). 파일/frontmatter 부재면 (빈 title, 텍스트)."""
    matches = sorted(decisions_dir.glob(f"{num:04d}-*.md"))
    if not matches:
        return "", ""
    text = fs.read_text_shared(matches[0], encoding="utf-8")
    m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    title = m.group(1).strip().strip("'\"") if m else ""
    return title, text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="contradiction_lint.py",
        description="모순 lint — 개정된 결정을 참조하는 문서의 잔여 모순 후보 표면화(advisory).",
    )
    p.add_argument("--new-adr", required=True, metavar="ADR-NNNN",
                   help="방금 발행/개정한 새 결정 id (decisions/ 에서 title·본문 로드)")
    p.add_argument("--amends", action="append", default=[], metavar="ADR-NNNN",
                   help="개정(amends) 대상 — 참조 스코프 수집 (반복 가능)")
    p.add_argument("--supersedes", action="append", default=[], metavar="ADR-NNNN",
                   help="대체(supersedes) 대상 — 참조 스코프 수집 (반복 가능)")
    p.add_argument("--show-prompt", action="store_true", help="LLM 탐지 프롬프트도 출력")
    return p


def _parse_ids(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        for part in re.split(r"[,\s]+", tok):
            part = part.strip()
            if part:
                n = adr_contradiction._adr_num(part) if part.upper().startswith("ADR-") else adr_contradiction._adr_num(f"ADR-{part}")
                if n is not None:
                    out.append(f"ADR-{n:04d}")
    return out


def main(home, argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    targets = _parse_ids(args.amends) + _parse_ids(args.supersedes)
    if not targets:
        print("[모순 lint] 개정 대상(--amends/--supersedes) 없음 — 신규 plain 발행은 참조 스코프가 "
              "없어 no-op(개정만 잔여 모순을 만든다).")
        return 0
    new_id = _parse_ids([args.new_adr])
    new_adr_id = new_id[0] if new_id else args.new_adr
    num = adr_contradiction._adr_num(new_adr_id) or 0
    title, text = _load_adr(pm_paths.decisions_dir(home), num)

    linter = adr_contradiction.ContradictionLinter(home)  # dry (LLM 미호출)
    result = linter.lint(
        new_adr_id=new_adr_id, new_adr_title=title, new_adr_text=text, target_ids=targets,
    )
    print(adr_contradiction.format_advisory(new_adr_id, targets, result))
    if args.show_prompt and result.prompt:
        print("\n── LLM 탐지 프롬프트 ──")
        print(result.prompt)
    return 0
