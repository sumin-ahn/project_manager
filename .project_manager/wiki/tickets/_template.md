---
id: T-NNNN
title: <한 줄 제목>
status: open                 # open | claimed | blocked | done — 디렉토리와 일치해야 함
created: YYYY-MM-DD
claimed_by:                  # 세션 식별자 (board.py claim 시 자동 채워짐)
claimed_at:
completed_at:
depends_on: []               # 선행 티켓 ID 들. 모두 done 되어야 claim 가능
blocks: []                   # 이 티켓이 끝나야 풀리는 후속들 (참조용)
touches:                     # 건드릴 파일·디렉토리 (다른 세션과 충돌 가능성 평가용)
  - path/to/file
estimate: small              # small | medium | large
tags: []                     # phase-1, infra 등 분류
---

# T-NNNN — <제목>

## 목표
무엇을 만들 / 바꿀 / 검증할지 1~3 문장.

## 인터페이스
이 ticket 이 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/계약.

## 결정
구현 방향에 대한 확정 사항 (어떤 방식으로 / 왜). 미정 사항은 "열린 질문" 으로.

## 완료 조건 (Definition of Done)
- [ ] 핵심 산출물 (파일, 동작)
- [ ] 단위 테스트 추가 (회귀 깨지지 않음)
- [ ] `.project_manager/wiki/status.md` 갱신 (해당 모듈 행)
- [ ] (해당 시) `.project_manager/wiki/log.md` append

## 참고
- [[architecture]] 관련 절
- 관련 ADR / spec: [[xxxxx]]
- 패턴 reference (이미 done 된 비슷한 ticket): T-XXXX

## 메모
작업 중 발견된 이슈, 결정, 다음 ticket 후보 등 — 끝나기 전 채워서 done 으로 옮길 것.
