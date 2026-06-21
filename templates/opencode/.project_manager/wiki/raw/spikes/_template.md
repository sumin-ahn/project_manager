<!--
설계 spike 템플릿 — 복제해서 새 설계 초안을 시작한다.

  1) 복제(단일·flat):       cp _template.md <주제-kebab>-$(date +%F).md
     한 주제 여러 산출/병렬:  mkdir -p <주제>/ && cp _template.md <주제>/<세션 또는 각도>-$(date +%F).md
            예) cp _template.md web-auth-redesign-2026-06-03.md
  2) 이 주석 블록은 지우고, frontmatter·섹션을 채운다. 빈 섹션은 삭제 가능.
  3) spike 는 sealed 후 IMMUTABLE — `status: draft` 동안은 편집/세션무관 resume 가능(같은 파일 이어쓰기),
     합의+사용자 사인오프 시 sealed (<date>). sealed 후 개정은 새 날짜 파일로(vN 누적). (ADR-0010 · raw/README.md)
  4) 병렬 OK: 파일명이 고유하면 여러 세션이 동시에 떨궈도 충돌 0.
     같은 날 같은 주제가 겹치면 파일명 끝에 -<세션명> 을 붙여 분리.
  5) 이 문서는 "결정"하지 않는다 — 근거 있는 권고 + 초안(ADR/ticket)까지.
     채택·발행·비준은 PM 이 §7 에서 한다.
-->
---
title: <한 줄 제목 — 무엇을 설계하는가>
created: <YYYY-MM-DD>
type: spike
status: draft                       # 합의·사인오프 시: sealed (<YYYY-MM-DD>)
session: <세션/에이전트 식별자>      # 예: orch-arch-<주제> · 직접 세션이면 그 이름
related: []                         # 연결할 ADR/ticket/spec/다른 spike. 예: [ADR-0025, T-0204, specs/api.md §10]
tags: [design-spike]
---

## 0. 한 줄 요약 + 권고

> PM 이 30초에 파악할 핵심: **무엇을 권고하는가 + 왜.** 본론은 아래에서.

## 1. 배경 / 현황 (read-only 실측)

<!-- 코드·데이터·문서에서 직접 확인한 사실. 추정과 실측을 구분하고 file:line 근거를 단다.
     모듈/레이어 경계·외부 계약이 걸리면 여기서 명시. -->

## 2. 옵션 비교 + 권고

<!-- 이 spike 의 핵심. 옵션 N개를 나열 → 각 장점/단점/비용 → 권고 1개를 명시한다. -->

### 옵션 A — <이름>
- 장점 / 단점 / 비용:

### 옵션 B — <이름>  (권고)
- 장점 / 단점 / 비용:

### 권고
<!-- 어느 옵션을 왜. 안전 가드·fail-soft·되돌릴 수 있는지(reversible) 영향을 한 줄로 짚는다. -->

## 3. 설계 상세 (해당 시)

<!-- DRAFT 스키마/DDL·인터페이스·다이어그램·의사코드. 전부 "DRAFT" 로 표시.
     기존 스키마/계약을 건드리면 마이그레이션 패턴도 DRAFT 로. -->

## 4. ADR 후보 (DRAFT — 발행은 PM)

<!-- 이 설계가 굳히려는 결정을 ADR 본문 초안으로. 번호는 PM 이 발행 시 부여.
     scope(PM-자율 internal·reversible / 사용자-게이트)와 결정 근거·기각 대안을 적는다. -->

## 5. ticket 분할안 (DRAFT — 발행은 PM)

<!-- 후속 구현을 ticket 단위로 쪼갠다. 각 항목에 touches(파일)·depends_on·DoD 가설.
     병렬 친화를 위해 touches disjoint 를 의식. -->

## 6. 위험 / 열린 질문 (사용자·PM 결정 필요)

<!-- spike 가 스스로 못 정하고 위로 올리는 것. 비용/외부송신/키발급/안전경계가 걸리면 여기. -->

## 7. 결정 · 후속 (설계 세션이 합의 시 채운다)

<!-- 설계 세션(사용자 + Claude)이 합의에 이르면 채우는 절: 합의 날짜 + 사용자 결정 +
     발행한 ADR/ticket 번호 역링크(발행 자체는 스킬 밖). 동시에 위 frontmatter status 를
     sealed (<date>) 로(설계 절 전부 합의 + §4·§5 완비 + 사용자 사인오프 시에만), related 에
     발행물 번호를 추가한다. 사인오프 전엔 draft 로 둔다 — 혼자 봉인 금지. (ADR-0010) -->
