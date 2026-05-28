---
title: Specs
created: {{DATE}}
updated: {{DATE}}
type: index
---

# Specs

> 자주 참조되는 **사양 / 포맷 / 한도** 의 단일 진실 소스.
> 설계 문서나 코드 주석에 흩어진 사양 디테일을 안정적인 한 곳으로 추출한다.

## 왜 분리하는가

- **설계 문서**(`raw/` 등) 는 시간 스냅샷 — 의도 / 결정 / changelog. immutable.
- **`decisions/`** 는 선택의 근거 — "왜 이렇게 정했나" ADR.
- **`specs/`** 는 현재 유효한 사양 — "지금 포맷이 정확히 이렇다", "지금 한도값이 정확히 이렇다".

ticket 본문 / 코드 docstring 에서 사양 디테일이 필요할 때:
- **사양** 이면 `specs/` 참조 (설계 문서 버전과 무관하게 항상 최신).
- **결정 근거** 이면 `decisions/` 참조.

## 현재 specs

| Spec | 출처 | 비고 |
|---|---|---|
| — | *(아직 없음)* | |

## 새 spec 추가 절차

1. `specs/<short-slug>.md` 파일 생성 (frontmatter `type: spec`)
2. 사양 본문 작성. 설계 문서에서 가져온 경우 "출처" 명시
3. 이 README 표에 행 추가
4. 관련 ticket / 코드 docstring 의 설계 문서 직접 참조를 `specs/<slug>` 참조로 변경
5. `log.md` 에 `## [YYYY-MM-DD] create | spec <slug>` append

## Spec 추출 트리거

- ticket 본문 / 코드 docstring 에서 설계 문서 절을 직접 참조하는 것을 발견할 때.
- 자주 변경되는 사양(포맷, 한도값)은 무조건 spec — 설계 문서 본문에 두면
  버전이 바뀔 때마다 동기화 부담이 생긴다.
