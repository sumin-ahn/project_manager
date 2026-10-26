#!/usr/bin/env python3
"""PM 핸드오프 7단계 자동화 헬퍼 — PM 세션 종료 시 기계 측정·편집 부분을 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/pm_handoff.py --task <task> --user-ack <task> \
      [--dry-run] [--no-pytest] [--ack-dirty "<사유>"] [--force-handoff]

    `--task` 를 생략하면 cwd 작업 폴더 → 단일 활성 task 순으로 스스로 해소하고, 그래도 정할
    수 없으면 중단한다. 차수와 wave 요약은 인자가 아니다 — task pm_state 에서만 추론한다.

    --dry-run 미리보기는 쓰기 0이라 --user-ack 없이 허용한다. 실제 핸드오프는 대상값에
    결속된 사용자 명시 승인 토큰이 필요하다.

동작 순서 (하나라도 실패하면 이후 단계 중단):
  0. dirty-tree 게이트 — 실행 앵커 트리(PM 홈 + 활성 worktree 전수)에 미커밋 잔여가 있으면
     **어떤 mutation 보다 먼저·회귀보다도 먼저** 중단(판정은 git 조회 몇 번). override =
     `--ack-dirty "<사유>"`(사유는 entry 에 박제). `--auto-trigger`는 사용자 명시 핸드오프
     호출부의 호환 신호일 뿐 독자 실행 권한이 아니며, ack 계약을 우회하지 않는다.
     task 가 보유한 작업 폴더의 커밋 안 한 변경은 사용자에게 "커밋할까요? 강제 핸드오프할까요?"
     를 묻고 멈춘다 — 넘는 수단은 `--force-handoff`(기록만·변경은 제자리·entry 에 기록) 하나다.
  1. 회귀 측정 — 게이트는 areas.md(prefix 행 > repo 행) > local.conf test.cmd 로 해소하고,
     해소 실패면 pytest tests/ -q. red 면 즉시 중단·핸드오프 불가(비-pytest 게이트는 exit
     code 로 판정 — §회귀 게이트 해소).
  2. log/current.md handoff entry skeleton append — 세션 중 박제 entry 자동 목록+메타학습·pending intent+회귀/incident.
  3. pm_state.md 세션 식별 표 sliding window 정리 — 신규 entry 추가 + 가장 오래된 entry 제거.
  4. pm_state.md 길이 검증 — wc -l 기준 700 라인 초과 시 warning.
  5. 인계 프롬프트 stdout 출력 — pm_playbook.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)"
     의 트리거(역할 framing + /pm-bootstrap)를 채워 stdout. 인계 본문은 log entry 가 이월 —
     부트스트랩이 자동 dump 하므로 프롬프트에 손-채움 안 함.
  6. git status dump — git status -s 출력 + 변경 파일 카운트 + 미push commit 수(ahead) 경고.
  6b. 미마감 raw sweep — 위임/추가 리뷰 raw 장부의 미마감 레코드 표면화(비차단 경고).
  7. 잔여 PM 수동 작업 출력 — checklist(커밋 + push 단계 포함).

결정:
  - subprocess DI: pytest/git subprocess 는 주입 가능한 함수로 감싼다.
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확 메시지).
  - 편집은 정규식 앵커 치환·멱등 — ticket_finish.py 와 동일.
  - LLM 미호출 — stdlib 만.
  - 인계 프롬프트는 stdout 만 — 파일 저장 안 함.
  - pm_state.md 슬라이딩 윈도우 = 3 차 (프로젝트별 조정 — SLIDING_WINDOW_SIZE).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.infra import console, pm_paths
from pm_engine.cli import handoff

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return handoff.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main())
