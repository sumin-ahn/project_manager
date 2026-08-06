---
title: Architecture Decision Records
created: {{DATE}}
updated: {{DATE}}
type: index
---

# Decisions (ADR)

> 명시화된 결정들. 대화·코드 주석에 흩어지지 않게 한 곳에 모아둔다. 형식: `NNNN-short-slug.md`.
> **lifecycle status**(frontmatter): `proposed` / `accepted`(live·완전 권위) / `amended`(여전히 유효하나
> 후속이 일부 개정·`amended_by` 가 가리킴) / `superseded`(완전 대체·비권위·`superseded_by`) / `deprecated`(철회).
> **개정 동사**: `refines`(추가·확장 → 대상 불변) ≠ `amends`(부분 수정 → 대상 `amended`) ≠ `supersedes`(완전 대체).
> ⚠️ 개정 시 **양쪽 갱신**(개정 ADR 의 `amends`/`supersedes` + 개정당한 ADR 의 status/`amended_by`) —
> `board.py lint` advisory 가 누락을 잡는다.

## Accepted (live · 완전 권위)

| # | Title | Date | Tags |
|---|---|---|---|
| — | *(아직 없음 — 첫 ADR 발행 시 이 표에 행 추가)* | | |

## Amended (유효 · 후속이 일부 개정)

| # | Title | amended_by | 무엇이 바뀌었나 |
|---|---|---|---|
| — | *(앞 ADR 이 후속에 의해 개정되면 Accepted→여기로 이동)* | | |

## 새 ADR 추가 절차

1. 다음 번호로 파일 생성: `decisions/NNNN-short-slug.md`
2. frontmatter — `title` / `created` / `updated` / `type: decision` / `status` / `scope` / `tags` (+ 개정 시 `amends`/`supersedes`/`refines`)
3. 본문: `## Context` / `## Decision` / `## Consequences` / `## References`
4. 이 README 표(Accepted)에 한 줄 추가
5. **앞 ADR 을 개정한다면 — lifecycle 갱신**: 개정 ADR 에 `amends:`/`supersedes:` + 개정당한 ADR 의 `status:`→amended/superseded·`amended_by:`/`superseded_by:` + 헤더 note + Accepted→Amended 표 이동. (`refines`=추가는 status 불변.)
6. `log/current.md` 에 `## [YYYY-MM-DD] decide | ADR-NNNN <slug>` append

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
