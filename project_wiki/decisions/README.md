---
title: Architecture Decision Records
created: {{DATE}}
updated: {{DATE}}
type: index
---

# Decisions (ADR)

> 명시화된 결정들. 대화·코드 주석에 흩어지지 않게 한 곳에 모아둔다.
> 형식: `NNNN-short-slug.md`. 상태는 frontmatter 에 (`proposed` / `accepted` / `deprecated` / `superseded`).

## Accepted

| # | Title | Date | Tags |
|---|---|---|---|
| — | *(아직 없음 — 첫 ADR 발행 시 이 표에 행 추가)* | | |

## 새 ADR 추가 절차

1. 다음 번호로 파일 생성: `decisions/NNNN-short-slug.md`
2. frontmatter — `title` / `created` / `updated` / `type: decision` / `status: proposed | accepted | deprecated | superseded` / `scope` / `tags`
3. 본문: `## Context` / `## Decision` / `## Consequences` / `## References`
4. 이 README 표에 한 줄 추가
5. `log.md` 에 `## [YYYY-MM-DD] decide | ADR-NNNN <slug>` append

## ADR 를 쓰는 시점

- 되돌리기 어렵거나 여러 모듈에 영향을 주는 구조 결정을 내릴 때.
- 같은 질문이 두 번째로 나왔을 때 (한 번 더 흩어지기 전에 박는다).
- PM 이 자율 결정한 내부 프로세스 변경 — `scope: internal-process` 로 기록.
- 미션·scope·핵심 경계를 바꾸는 결정 — `scope: mission`, 사용자 동의 필수.

## scope 분류

- `internal-process` — 프로세스·네이밍·내부 구조. PM 자율 + 사후 로그.
- `mission` — 프로젝트 미션·scope·핵심 안전 경계. 사용자 사전 동의 필수.

`[[ADR-NNNN]]` wikilink 로 다른 문서에서 참조한다. 파일명(`NNNN-slug.md`)과
`ADR-NNNN` 표기가 다르므로, 참조 일관성은 운영 규율로 유지한다.
