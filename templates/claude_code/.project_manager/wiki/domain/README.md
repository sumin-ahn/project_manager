---
title: Domain — 살아있는 프로젝트 지식
type: index
updated: {{DATE}}
---

# Domain — 살아있는 프로젝트 지식

> 이 프로젝트가 **무엇이고 어떻게 다루나**의 *살아있는* 지식.
> (대비: [`decisions/`](../decisions/) = *왜 결정했나*·동결. 여기 `domain/` = *현재 무엇·어떻게*·계속 갱신.)

## 페이지 = 개념 그래프
- 한 페이지 = **한 가지**(모듈·엔티티·작업방법·개념).
- `[[다른-페이지]]` 로 링크 → 그게 곧 그래프(별도 포맷 없음 · wikilink lint 가 검증).
- 종류(frontmatter `type:`): `concept`(무엇·왜 이 모양) · `guide`(어떻게 하나·절차) · `research`(조사·누적).

## frontmatter (Phase 1 게이트가 읽는 계약)

```
---
title: <한 가지>
type: concept | guide | research
covers:                 # 이 페이지가 담당하는 코드 (글롭). 코드-무관 개념이면 비움.
  - src/foo/**
derived: false          # true = 코드서 자동생성(손대지 마) · false = 사람 author
updated: YYYY-MM-DD
---
```

## 살아있게 — 계속 자란다
- **처음부터 완벽 불요.** 업무 때 그때그때 갱신하며 자란다.
- `covers` 코드를 건드리는 ticket 은 그 페이지 갱신을 *알림* 받는다 (soft·friction 0).
- `covers` 코드가 페이지 `updated` *후*에 바뀌면 = **stale** → 쓸 때 ⚠ 표시(맹신 차단)·freshness lint 포착. *막지 않고 보이게* — 틀린 정보 조용히 참조 방지.

## guide → skill
`guide` 는 `.md` 로 시작. 실행 스크립트/템플릿 번들이 필요해지면 그때 `SKILL.md` 로 graduate (점진로드).

## 쓰는 법
[`_template.md`](_template.md) 복사 → 채움. **한 가지만·짧게·링크 많이.** 그래프는 `[[ ]]` 가 만든다.

## CLI (domain.py)
```
{{PY}} .project_manager/tools/domain.py list                 # 페이지 카탈로그 (type·covers·stale)
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN   # ticket touches 와 겹치는 covers 페이지
{{PY}} .project_manager/tools/domain.py capture --tickets T-NNNN   # 갱신 reminder (touch∩covers)
{{PY}} .project_manager/tools/domain.py lint                 # freshness(stale) 검사
```

> ℹ️ 살아있는 루프: 코드 touch → 겹치는 페이지 **소환**(`affected`) → 갱신 reminder(`capture`) → 채록 → stale ⚠ 는 `lint` 가 가시화. 막지 않고 보이게.
