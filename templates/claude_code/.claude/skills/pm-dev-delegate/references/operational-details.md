# /pm-dev-delegate 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

#### 리뷰 격리 스냅샷 (엔진 소유)

격리 스냅샷은 **엔진이 만든다** — 묶음 리뷰 위임(`--role code-reviewer --cluster`)이 장부의 묶음
브랜치 tip 에 결속해 저장소 밖에 스냅샷을 만들고, 실행 root 로 준 뒤, 끝나면 등록까지 정리한다.
PM 이 스냅샷을 만들거나 지우는 손절차는 없다.

- **결속 판정**은 두 번이다 — 입력을 해소할 때와 스냅샷을 만들기 직전. 트리가 묶음 브랜치가
  아니거나 리뷰 대상 파일에 커밋되지 않은 변경이 있으면 거부한다. 프롬프트가 싣는 diff 와 모델이
  읽는 파일이 갈리면 판정 전체가 헛돌기 때문이고, 그래서 이 거부는 완화 인자가 없다.
- **커밋도 엔진이 한다** — developer 라운드를 돌려받는(harvest) 성공 경로에서 그 슬롯의 코드
  변경이 티켓 제목을 문안으로 커밋된다(변경이 없으면 커밋도 없다). 그래서 리뷰를 띄우는 시점의
  트리는 이미 확정돼 있고, PM 이 리뷰 전에 손으로 스테이지·커밋하는 절차는 없다.
- **범위**는 `merge-base(<통합 브랜치>, <묶음 브랜치>)..<묶음 브랜치>` 의 변경 파일이다. 삭제분은
  스냅샷 범위에 넣지 않는다(현재 트리에 대조할 파일이 없다) — 삭제 자체는 diff 본문에 실린다.
- 생성 실패는 **송신 전** 차단이다. 실패 사유는 생성기가 그대로 낸다(스냅샷 없는 리뷰로 강등하지
  않는다).
- 추가 리뷰어 채널(`external_review.py`)은 staged diff 기반이라 이 스냅샷 축과 별개다 — 검토 파일을
  다시 `git add` 한 뒤 실행하는 규율은 그대로다.

## 병렬 wave touches cross-check

dev N 동시 spawn 전 disjoint 확인은 **엔진이 계산한다** — 손으로 대조하지 않는다.

- `pm_delegate --role developer --ticket T-NNNN`(dry-run 포함)은 전송 전에 **같은 세션이 claim
  중인 다른 ticket** 의 touches 와의 교집합을 stderr 1블록으로 낸다:
  `=== ⚠ 병렬 위임 touches 겹침 ===` + `- T-XXXX(<상대 선언>) ∩ T-NNNN: <겹친 경로>`.
  차단하지 않으므로 그 블록을 읽고 PM 이 판정한다.
  - 겹치면: 순차 실행하거나 `pm-config worktree add <repo>` 로 슬롯을 분리한다. 공통 통합
    파일의 함수 단위 추가는 완화 조건으로 허용하고, 같은 함수·같은 줄 동시 수정은 차단한다.
  - 블록이 없으면 겹침 0이다. `경고: 병렬 위임 touches 교집합 계산 실패 …` 1줄이 보이면
    판정 불능이므로 그때만 손으로 대조한다.
  - 판정 축은 `claimed_by` 값 일치다 — 다른 사용자/다른 task 의 claim 은 계산에서 빠진다.
- 위임 회수 시 `경고: 겹친 파일 변경됨 — 다른 dev 산출 공존 여부를 확인하라: <경로>` 가 나오면
  그 파일에 두 dev 산출이 공존하는지 확인한 뒤 리뷰/커밋한다.
- baseline 회귀는 dev cycle 후 한 번에 측정(race 회피).

## reviewer 후 처리

- **accepted finding**: reviewer 수정·테스트 계약을 그대로 fix 입력으로 전달한다.
- **rejected/suggestion**: board 의무로 만들지 않고 현재 판정에서 닫는다.
- **결정 필요**: board를 쓰지 않고 사용자에게 목표 확대 여부를 묻는다.

reviewer 결과를 그대로 믿지 말고 should-fix 전 코드 흐름을 PM이 독립 점검한다. 부정확하면 변경하지 않고 `log/current.md`에 영구 기록. 다른 ticket 결함을 현재 ticket 영역으로 잘못 attribute할 수 있으므로 실제 영역 확인 후 분기한다.

## 운용

- board 조작은 PM, 서브에이전트는 구현/검토만.
- dev 보고에는 변경 파일, 신규 테스트, 단계별 지정 회귀, DoD별 evidence를 강제한다. fix는 전체 회귀까지 실행한다.
- background 중에는 현재 티켓의 읽기 전용 근거만 정리하고 board를 쓰지 않는다.
- 프롬프트가 길어지면 ticket 본문을 보강한다.
- 같은 dev를 반복 resume하면 transcript 누적으로 컨텍스트 한도에 실패한다(14회 resume에서 "Prompt is too long"). 대략 5~6회↑면 새 에이전트에 자족 프롬프트로 재투입하고 현 코드 상태(신설 심볼·미커밋 변경)를 요약한다. 산출물은 워킹트리에 유지된다.

## 참고

- `.project_manager/wiki/pm_role.md` — wave 패턴·dev/reviewer cycle·must-fix 분기
- `.project_manager/tools/pm_delegate.py` — cross-harness 위임
- `.claude/agents/developer.md`
- `.claude/agents/code-reviewer.md`
