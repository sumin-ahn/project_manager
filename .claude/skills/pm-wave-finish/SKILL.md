---
name: pm-wave-finish
description: "wave 안 ticket 완료 부기 — ticket_finish.py wrapper + 회귀 측정 + status.md 스칼라 + log/current.md skeleton + board complete + git stage. 모듈 행 비고·log/current.md 서술·git commit 은 PM 손. Triggers: 'T-NNNN 완료', 'ticket 정리', 'finish', 'pm-wave-finish'."
---

# /pm-wave-finish T-NNNN <섹션> — wave ticket 완료 부기

> {{PROJECT_NAME}} PM wave 안 ticket 완료 시 부기 자동화. backbone =
> `.project_manager/tools/ticket_finish.py`. 본 skill 은 호출 chain
> + PM 손 잔여 작업 안내.

## 사용 시점

dev/reviewer cycle 통과 (must-fix 0) 또는 PM 직접 구현 ticket 완료 시.

## 실행

```bash
{{PY}} .project_manager/tools/ticket_finish.py T-NNNN --section "<섹션명>"
```

`<섹션명>` 인자 = status.md 의 모듈 매트릭스 행 식별 키. 회귀 카운트가 어느 섹션의
*합계* 에 들어가는지 지정.

## CLI 자동 처리

1. **회귀 측정** — `pytest tests/ -q`. red 면 즉시 중단 (반려 → dev 재작업 필요).
2. **status.md 스칼라 갱신** — 전체 테스트 수·섹션 합계·회귀 라인 (`A / B passed`) + 섹션의 인라인 소계 행 (있으면).
3. **log/current.md complete entry skeleton append** — `## [YYYY-MM-DD] complete | T-NNNN — <title>` 형식. 본문 = `<PM: 무엇을·왜>` placeholder.
4. **board.py complete T-NNNN** — `--tests-pass` 가드 통과 후 status open→done.
5. **git stage** — 변경 파일 자동 `git add`. commit 은 별도 (PM 손).

## 잔여 PM 손작업 (CLI 후)

1. **status.md 모듈 행 비고 갱신** — 해당 ticket entry 추가. CLI 가 자동화 안 함 (의도적 경계 — 모듈 행 = PM 손).
2. **log/current.md complete entry 본문 서술** — skeleton 의 `<PM: 무엇을·왜>` 를 실제 내용으로:
   - 변경 파일 목록
   - 단위 테스트 수·증가량
   - dev/reviewer cycle 요약 (must-fix·should-fix 처리 분기)
   - PM 직접 처리 should-fix (1줄·dev 안 도는 영역)
   - 메타 학습 (wave 다음 단계·후속 ticket 후보)
   - spec/ADR 정합 갱신 (있으면)
3. **git commit** — 메시지: `T-NNNN — <title 요약>` (또는 wave 단위 단일 commit·복수 ticket 일 때).
4. **wave 진행 중이면 다음 ticket** — `/pm-wave-claim` 으로 다음.
5. **wave 종결이면 wave 메타 entry append** — pm_playbook.md §"Wave 메타 학습 누적" 표준.

## 결정

- **모듈 행·commit 자동화 안 함 (의도적)** — 단일 진실 파일 직접 편집·자동 commit 의 부수 영향 회피. *자동화는 잡일까지·서술/commit 은 PM 손* 패턴 정합.
- **fail-soft 가 아니다** — 회귀 red 시 즉시 중단. ticket complete 차단 (board.py complete 의 `--tests-pass` 가드).
- **wave 종결 commit message 형식** — `PM 세션(N차) wave M — <ticket 목록> + <핵심 메타 학습 요약>`. wave 단위 단일 commit (history bisect/cherry-pick 어려움 trade-off).

## 참고

- `.project_manager/tools/ticket_finish.py` — backbone CLI
- `.project_manager/wiki/pm_role.md` — wave 패턴 단일 진실
