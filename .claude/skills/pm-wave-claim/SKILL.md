---
name: pm-wave-claim
description: "wave 안 ticket claim — board show + DoD self-containment PM 검증 + claim. ticket 본문에 placeholder / depends_on 미충족 / wikilink dangling 있으면 차단. Triggers: 'T-NNNN claim', 'ticket 잡기', 'wave 시작', 'pm-wave-claim'."
---

# /pm-wave-claim T-NNNN — wave 시작 ticket claim

> {{PROJECT_NAME}} PM wave 시작 시 ticket 1개를 자율 claim 하는 표준 절차. PM 의
> ticket self-containment 검증을 *trigger 단위 강제* 한다.

## 실행

```bash
# 1. ticket 본문 dump
{{PY}} .project_manager/tools/board.py show T-NNNN

# 2. lint (의존성 일관성)
{{PY}} .project_manager/tools/board.py lint

# 3. PM 검증 (아래 체크리스트)

# 4. 통과 시 claim
{{PY}} .project_manager/tools/board.py claim T-NNNN --session pm
```

## PM 검증 체크리스트 (claim 전)

다음 항목 *전부 충족* 시에만 claim. 하나라도 실패하면 ticket 본문 보강 우선
(pm_playbook.md §메타 정책 "Ticket 본문" — self-contained 의무).

- [ ] **표준 섹션 6개 존재** — 목표 / 인터페이스 / 결정 / 완료 조건 / 참고 / 메모. board.py lint 가 표준 섹션 누락 차단·`<...>` placeholder 0개.
- [ ] **depends_on 모두 done** — 의존 ticket 이 아직 open/claimed 면 차단. blocked 의존은 reason 확인.
- [ ] **touches 명시** — wave 병렬 시 touches disjoint 안전성 검증 substrate. 누락 시 보강.
- [ ] **wikilink dangling 0개** — `[[name]]` 참조가 실제 존재하는 페이지·메모리·ADR·ticket 인가 (lint 또는 별도 회귀 가드).
- [ ] **DoD verify-able** — *충족 evidence 측정 방법* 이 ticket 본문에 명시 (테스트 + 단위 수·라이브 검증 절차·spec 정합 확인 등).
- [ ] **컨텍스트 예산** — touches 에 대형 파일이 있거나 이해에 광범위 읽기가 필요하면 dev(cold subagent) 컨텍스트 truncation 위험. 분할하거나 본문에 정확한 함수/라인·패턴 reference 를 박아 dev 읽기 범위를 좁힌다 (본문 = dev 컨텍스트 방화벽).
- [ ] **PM 자율 vs 사용자 게이트 분류** — {{PROTECTED_PATHS}} / mission scope / 외부 비가역 행위 영향 시 사용자 게이트 통과 확인 (pm_role.md §사용자 게이트).

## 자율 claim 가능 ticket 후보

PM 자율 영역 (pm_role.md §"자율 + 사후 로그"):
- `scope: internal-process` ADR 산출 ticket
- 핵심 안전 경계·자본·외부 비가역 무영향·가역
- 사용자 게이트 항목 무관

사용자 게이트 후보 ticket 은 *claim 보류 + 사용자 결정 대기*.

## 실패 분기

- **본문 부족** → PM 직접 본문 보강 (cold dev 가 본문만 보고 시작 가능해야).
- **depends_on 미충족** → 의존 ticket 진행 우선 또는 본 ticket blocked 처리.
- **lint warning** → 의존성 모순 fix.
- **wikilink dangling** → log/current.md 의 메타 entry 인용은 link 형태 또는 raw 단어로 재작성.

## 결정

- **PM 검증은 손작업** — 자동화 부분은 board.py lint 가 placeholder·표준 섹션·순환 의존만. DoD verify-able·본문 self-containment·게이트 분류는 PM 인지.
- **wave 안 1 ticket 1 claim** — 동시 다중 claim 회피 (touches conflict 위험·orchestrator 단순화).
- **dev/reviewer 위임 ticket 도 PM claim** — board.py claim 은 orchestrator(PM) 영역 (pm_playbook.md §"위임 — 두 가지 방식" 방식 A). 서브에이전트는 구현/검토만.

## 참고

- `.project_manager/wiki/pm_role.md` — ticket 본문 self-contained 의무·claim 워크플로
