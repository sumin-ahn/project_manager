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
  - 구현은 `pm_engine.domain.adr_rules`(순수 규칙)·`pm_engine.app.adr_issue`(채번·발행), 명령 파서와
    배선은 `pm_engine.cli.adr_cmd` 이고 이 파일은 진입이다.
  - LLM 미호출 — stdlib + PyYAML(대상 frontmatter 파싱)만.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import adr_cmd
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return adr_cmd.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
