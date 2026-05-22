---
title: Architecture
created: {{DATE}}
updated: {{DATE}}
type: architecture
---

# Architecture

> 구조 + 모듈 매핑 + 모듈 간 계약. **진행 상태**는 [`status.md`](status.md) 에서,
> 여기서는 **무엇이 무엇에 의존하는가** 만 다룬다.

## 0. 한 줄 요약

<!-- TODO: 시스템 데이터 흐름을 한 줄로. 예:
     입력 → 처리 계층 → 결정 계층 → 출력/저장 -->

## 1. Layer / 모듈 개요

<!-- TODO: 프로젝트를 Layer 또는 컴포넌트 그룹으로 나눠 기술한다. -->

```
Layer 0 — ...
Layer 1 — ...
Layer 2 — ...
```

## 2. 모듈 의존성

<!-- TODO: 모듈별 imports / imported-by 표. 순환 의존을 드러내는 것이 목적. -->

| 모듈 | imports | imported-by |
|---|---|---|
| (예시) `core.py` | — | — |

## 3. 모듈 간 계약

<!-- TODO: 모듈 경계의 데이터 형식·인터페이스 약속. 자주 변하는 사양은
     여기 두지 말고 specs/ 로 추출한다. -->
