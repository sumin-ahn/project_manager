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

설계(구현은 `pm_engine.app.adr_contradiction`, CLI 는 `pm_engine.cli.contradiction_lint`, 이 파일은 진입):
  - `collect_reference_scope` — 파일 목록 주입. 기본 파일 수집은 `wikilink_lint._collect_wikilink_files`
    재사용, 읽기 실패 시 graceful 빈 스코프.
  - `ContradictionLinter` — DI(files_fn·run_fn) 로 hermetic 테스트. `run_fn=None`(기본)이면 dry — 프롬프트만
    산출·LLM 미호출.
  - 이 lint 는 **advisory** — 어떤 경로에서도 예외로 발행을 막지 않는다(fail-soft·pm_adr 트리거는 감싸 호출).
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import contradiction_lint
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return contradiction_lint.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
