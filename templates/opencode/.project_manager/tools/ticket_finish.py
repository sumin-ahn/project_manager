#!/usr/bin/env python3
"""PM 기록 자동화 헬퍼 — ticket 완료 시 기계적 기록을 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/ticket_finish.py T-NNNN [--section "<섹션명>"] [--dry-run]
    venv/bin/python .project_manager/tools/ticket_finish.py --cluster C-<ticket-group> [--dry-run]
        묶음 종결(close) — 아래 다섯 단계를 티켓마다 돌리고 그 앞뒤로 기계 확인·게이트 처분·
        커밋·재배치·머지·board 기록까지 고정 순서로 닫는다(재실행은 재개). 슬롯은 반납하지 않는다.

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 실행 — 게이트는 areas.md(prefix 행 > repo 행) > local.conf test.cmd 로 해소하고,
     해소 실패면 pytest tests/ -q. red 면 즉시 중단(비-pytest 게이트는 exit code 로 판정
     — §게이트 종류).
  2. log/current.md 스켈레톤 append — 표준 형식 entry 골격.
  3. board.py complete 호출 — 회귀를 이미 통과했으므로 --tests-pass.
  4. git stage — **선언 경로만**(ticket `touches` ∪ *이 실행이 쓴* 산출물) 스테이징 + 스코프
     밖 잔여 dirty loud 보고. commit 은 PM 이 한다.
  5. 잔여 PM 수동 작업 출력.

결정:
  - subprocess DI: pytest/git/board.py subprocess 는 주입 가능한 함수로 감싼다.
  - red 면 중단: log/current.md / board / git 어떤 것도 건드리지 않는다.
  - status.md 는 더 이상 건드리지 않는다 (status.md = judgment-only).
    테스트 수·합계·소계·회귀 실측은 derivable(pytest/board.py regression 실측·log history)이라
    status.md 에 손으로 박제하지 않는다. 이 도구는 status.md 미접촉.
  - 모듈 행·서술·commit 은 자동화하지 않는다 (v1 축소판 — §배경).
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확한 메시지).
  - LLM 미호출 — stdlib + pm_engine 패키지만.

이 파일은 안정된 CLI 진입점이다 — 콘솔 설정과 설치 앵커(`REPO`)의 Home 만 넘기고, 기록·종결 흐름의
본체는 `pm_engine.cli.ticket_finish`(argv·배선)·`cli.finish_record`(완료 기록)·
`cli.cluster_close`(묶음 종결)와 finish_* 소유 모듈에 있다.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import ticket_finish
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return ticket_finish.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
