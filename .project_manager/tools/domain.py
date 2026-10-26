#!/usr/bin/env python3
"""domain 레이어 CLI — 페이지 covers 인덱스 조회·채록·초안·coverage·lint (Phase 1 #1).

`domain/` 페이지 frontmatter `covers:`(담당 코드 글롭)로 *페이지 ↔ 코드* 인덱스를 만들어
touches∩covers 매칭·staleness·채록에 쓴다. 이 파일은 CLI 진입(`list`/`affected`/`capture`/
`capture-draft`/`coverage`/`lint`)이고 명령 파서·배선은 `pm_engine.cli.domain_cmd`, 구현은 패키지에
있다: 문서 규칙 `pm_engine.domain.doc_pages`·
초안 양식 `pm_engine.domain.doc_drafts`, 페이지 읽기·쓰기 `pm_engine.infra.doc_page_store`, 판정
`pm_engine.app.doc_staleness`·`doc_affected`·`doc_coverage`·`doc_lint`.

capture(채록)는 surface-only — 담당 페이지·coverage gap 을 *띄울* 뿐 본문 자동 생성/스탬프는
안 한다(자동 `updated:` 는 stale 탐지를 거짓으로 만듦). capture-draft 는 researcher 조사 prose 를
domain 초안(`status: draft`)으로 *scaffold* 한다 — prose 는 verbatim 배치(요약/구조화 금지)·
**git 무조작**(add/commit 0)·promote(draft→정식)는 사람 손.

domain 페이지·engine.manifest 경로는 스크립트 위치의 Home 에서 해소해 패키지 함수에 명시로 넘긴다.
ticket touches는 실행 ticket_store와 명시 Home으로 읽는다.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import domain_cmd
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return domain_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
