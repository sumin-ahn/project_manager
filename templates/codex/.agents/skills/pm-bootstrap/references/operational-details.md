# $pm-bootstrap 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 출력 검증

- **board**: `done / open / claimed / blocked`는 내 세션 스코프(open=내 세션 생성 스트림, claim=내 세션)다. 타 세션분은 기본 dump에 없다. 공유 풀 전체는 `board.py list --all`, 내 전 세션은 `--mine`으로 조회한다. 숫자는 스냅샷이므로 옵션 제시 전 `board.py list --mine`으로 claim 주체를 교차 확인한다(부분 push/오프라인 창).
- **회귀**: default는 `(skip — handoff entry 참조 · --with-pytest 로 재측정)`. `--with-pytest`면 `N / N passed`; red면 즉시 baseline fix하고 wave 시작을 막는다.
- **git**: 브랜치, 최근 5 commit, working tree clean 여부. task-only는 task 소유 작업공간만 수집하고 다중 슬롯 대표 cwd·전수 freshness 범위를 표시한다.
- **차수**: `## PM N차 부트스트랩`. task pm_state 또는 bound slot pm_state의 세션식별 절에서 추론하며 미해소면 placeholder다.
- **마지막 entry**: `log/current.md` 제목(date·type·title) + 본문 전체를 `<details>`로 dump한다. `handoff`면 직전 PM 종료 정합, `complete`면 wave 진행 중일 수 있다.
- **pm_state**: 남은 작업/사용자발의 절을 surface한다.

## PM 보고

pm_role.md §인계 후 첫 turn template에 따라 CLI 출력 뒤 다음만 요약·판단한다:

1. board 1줄: `done / open / claimed / blocked` + 회귀·lint·git.
2. 직전 세션 3~5줄: dump된 handoff의 핵심 산출물·메타 학습.
3. 다음 옵션 N개: surface된 pm_state 남은 작업 전체 그림의 우선순위.
4. 결정 요청: *무엇부터 갈까요?* + 권장 시퀀스 1줄.

CLI subprocess 실패는 fail-soft가 아니므로 즉시 중단·보고한다. 이 skill은 비즈니스 로직 없는 thin wrapper이며 자동 trigger는 frontmatter description의 한국어 명령(예: `"부트스트랩"`)으로 매칭한다.

참고: `.project_manager/tools/pm_bootstrap.py`(backbone), `.project_manager/wiki/pm_role.md`(부트스트랩 절차 단일 진실).
