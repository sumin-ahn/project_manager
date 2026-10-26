#!/usr/bin/env python3
"""모순 lint — "결정을 바꾼 순간의 잔여" 탐지.

결정을 개정(amends/supersedes)하면 옛 결정을 전제로 쓰인 다른 서술(타 ADR·wiki·spike·프롬프트)이
새 결정과 **모순된 채 남는다**. 기계 lint 전 구간이 clean 인데도 이 잔여는 안 잡힌다.
병목은 *가드 부재*가 아니라 **인지 시점** — 재정의를 하는 바로 그 순간에 점검해야 놓치지 않는다. 이 lint 는
그 순간(ADR 발행/개정 명령·pm_adr.py)에 배선돼 발화한다.

역할 분업:
  - **탐지 = LLM** — prose 의미 모순은 정규식으로 못 잡는다(LLM 필요). 단 호출은 **DI seam + 기본 dry
    (미호출)** — 기본은 스코프를 기계로 수집하고 LLM 프롬프트를 산출물로 표면화한다(비용 없음·hermetic).
    실 LLM 배선이 필요하면 `run_fn` 을 주입한다(additional_reviewer DI 동형).
  - **판정 = 사람** — 실제 모순인지 판정·해소는 사람(generate≠evaluate). **차단 아님**(advisory·
    후보 표면화까지가 lint 역할).

대상 스코프:
  - 개정된 결정(`target_ids`)을 `[[ADR-NNNN]]` **wikilink 로 참조하는 문서**(back-ref 범위). 전-코퍼스
    무차별이 아니다. wikilink 문법·파일 수집은 `pm_engine.app.wikilink_lint` 의 것을 재사용한다(자체 regex/매핑 신설 0).

설계(구현은 `pm_engine.app.adr_contradiction`, 이 파일은 CLI 진입):
  - `collect_reference_scope` — 파일 목록 주입. 기본 파일 수집은 `wikilink_lint._collect_wikilink_files`
    재사용, 읽기 실패 시 graceful 빈 스코프.
  - `ContradictionLinter` — DI(files_fn·run_fn) 로 hermetic 테스트. `run_fn=None`(기본)이면 dry — 프롬프트만
    산출·LLM 미호출.
  - 이 lint 는 **advisory** — 어떤 경로에서도 예외로 발행을 막지 않는다(fail-soft·pm_adr 트리거는 감싸 호출).
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
from pm_engine.infra import console, fs, pm_paths  # noqa: E402
from pm_engine.app import adr_contradiction  # noqa: E402


# ── REPO 앵커 (설치 깊이 고정 · hermetic 테스트 monkeypatch seam) ──────────
# 설치 깊이는 `pm_import` 가 못 박는다 — 도구는 `<root>/.project_manager/tools/` 에만 놓인다.
# REPO 는 module-level 상수로 유지해 hermetic 테스트가 monkeypatch 할 수 있게 한다
# (각 파일 self-contained — 공유 import 미도입).

def _find_repo_root() -> Path:
    """엔진 루트 — 도구는 언제나 `<root>/.project_manager/tools/` 에 설치된다.

    상향 탐색을 하지 않는다. 설치 경로를 만드는 쪽(`pm_import`)이 그 깊이를 못 박으므로
    다른 깊이는 나올 수 없고, 탐색은 합성 트리에서 **자기 위 실 인스턴스**를 답으로 주는
    부작용만 낸다(실측: 실 PM 홈 log 오염·등록 안 된 worktree 가 등록으로 판정).
    나머지 도구 20곳과 같은 규칙이다.
    """
    return Path(__file__).resolve().parents[2]


REPO = _find_repo_root()
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"


# ── CLI (standalone 재검사·pm_adr 는 API 로 직접 호출) ──────────────────────────────

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


def main(argv: list[str] | None = None) -> int:
    console.configure_console_utf8()
    args = build_parser().parse_args(argv)
    targets = _parse_ids(args.amends) + _parse_ids(args.supersedes)
    if not targets:
        print("[모순 lint] 개정 대상(--amends/--supersedes) 없음 — 신규 plain 발행은 참조 스코프가 "
              "없어 no-op(개정만 잔여 모순을 만든다).")
        return 0
    new_id = _parse_ids([args.new_adr])
    new_adr_id = new_id[0] if new_id else args.new_adr
    num = adr_contradiction._adr_num(new_adr_id) or 0
    title, text = _load_adr(DECISIONS_DIR, num)

    linter = adr_contradiction.ContradictionLinter(pm_paths.home_at(REPO))  # dry (LLM 미호출)
    result = linter.lint(
        new_adr_id=new_adr_id, new_adr_title=title, new_adr_text=text, target_ids=targets,
    )
    print(adr_contradiction.format_advisory(new_adr_id, targets, result))
    if args.show_prompt and result.prompt:
        print("\n── LLM 탐지 프롬프트 ──")
        print(result.prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
