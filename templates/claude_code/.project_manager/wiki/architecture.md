---
title: Architecture — 현재-아키텍처 단일 진실 (live / target)
created: {{DATE}}
updated: {{DATE}}
type: architecture
status: live
---

# Architecture — 현재-아키텍처 단일 진실

> 이 문서 = **현재-아키텍처 단일 진실** (부트스트랩 1순위 · ADR-0022). 두 절로 나뉜다:
> **① live (real)** = *지금 코드에 실제로 있는* 구조. **② target (확정·미구현)** = 결정됐으나 아직
> 코드에 없는 방향. ①/② 분리로 "결정됐지만 미구현"이 "현재"로 둔갑하는 것을 막는다.
>
> **ADR(`decisions/`) = *왜*의 히스토리** (근거 · 현재 구속력 없음). 현재-기준은 이 문서 하나다.
> 충돌 시 *현재 의도/실측 > 옛 ADR* → 이 문서 갱신 + ADR amend/supersede (요구를 옛 ADR 에 재해석 ✗).
> **관리 = architect**(코드 대조·라이브 결선/완성 평가 = 설계 노동 · 새 ADR / wave 후 / drift 의심 시 갱신)
> · **PM = 점검**(저자 아님). 세부 지식은 [`domain/`](domain/README.md) 페이지로(covers→코드 추적), 이 문서는 overview.

---

## ① 현재 라이브 (real) — 코드 실측

<!-- 지금 코드/repo 에 *실제로 있는* 구조·모듈·의존성·계약. 코드 대조로 검증(없으면 ①에 넣지 마라).
     성장 모델: 처음엔 coarse, 업무하며 자란다. -->

## ② 확정·미구현 (target) — 결정됐으나 코드에 없는 방향

<!-- ADR 로 결정됐으나 아직 구현 안 된 항목. 구현되면 ①로 승격·여기서 제거. -->

## 참조

- 결정 히스토리(왜): [`decisions/`](decisions/) — ADR (근거·현재 구속력 없음)
- 세부 지식(무엇·어떻게): [`domain/`](domain/README.md)
