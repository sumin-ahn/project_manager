---
title: Ideas
created: {{DATE}}
updated: {{DATE}}
type: index
tags: [ideas, backlog]
---

# Ideas

아직 결정되지 않은 후보 (`pre-ADR`) 들이 모이는 곳.

[`decisions/`](../decisions/) 가 **이미 결정된 것** (ADR — Accepted) 이라면,
여기는 **익히고 있는 것**. 한 idea 는 셋 중 하나로 끝난다:

- **promoted** → ADR 로 승격되거나 ticket 으로 분해됨
- **killed** → 지금 안 한다고 결정됨 (사유 본문에 남김)
- **open** → 아직 익는 중

## 파일 규칙

- **디렉토리 = status.** idea 는 `open/` `promoted/` `killed/` 중 하나에 산다
  (`tickets/` 와 동형). 이 `README.md` 만 `ideas/` 직속 (인덱스).
- 파일명: `NNNN-short-slug.md` (4자리 zero-pad, 슬러그는 영문 kebab-case)
- frontmatter 필수: `id / title / created / updated / type: idea / status / tags`
- 본문 권장 섹션 (`board.py idea new` 가 골격 생성):
  1. 한 줄 요약
  2. 동기 (왜 끌리는가)
  3. 가능한 구현 형태 (high-level)
  4. 위험 / 고민거리
  5. 열린 질문
  6. 다음 행동 (promote / kill 기준)
  7. 관련 링크

## 운영 워크플로

idea 의 상태 전이는 `board.py` 가 단독으로 관리한다 — **수동 `mv` 금지**
(디렉토리 ↔ frontmatter `status` drift 방지).

```bash
{{PY}} .project_manager/tools/board.py idea list [--status open|promoted|killed]
{{PY}} .project_manager/tools/board.py idea new "<title>" [--tag a,b]
{{PY}} .project_manager/tools/board.py idea promote NNNN   # open/ → promoted/
{{PY}} .project_manager/tools/board.py idea kill NNNN      # open/ → killed/
```

1. **새 idea** → `idea new "<title>"` 가 `open/NNNN-slug.md` 를 채번·생성한다.
2. **익으면 (promote)** → ADR 작성 또는 ticket 분해 후 `idea promote NNNN`.
   본문 하단에 ADR / ticket 링크를 추가한다. **파일은 지우지 않는다** (역사 보존).
3. **죽이면 (kill)** → `idea kill NNNN` 후 본문에 "사유" 섹션을 추가한다.
4. ticket 으로 분해되었다면 ticket 본문 "참고" 섹션에서 idea 를 역참조.

`board.py lint` 가 디렉토리 ↔ frontmatter `status` 일치를 검사한다.

## 현재 idea 목록

### Open (익히는 중)

| ID | 제목 |
|---|---|
| — | *(아직 없음)* |

### Promoted (실현·분해됨 — 파일은 역사 보존)

*(없음)*

### Killed

*(없음)*
