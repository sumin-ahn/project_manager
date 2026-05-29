---
title: PM Role (Project Manager Session)
created: {{DATE}}
updated: {{DATE}}
type: handoff
---

# PM Role — Project Manager Session 인계 문서

> 이 페이지는 **PM 세션이 매 시작 시 첫 번째로 봐야 할 인계 문서**.
> 개별 ticket 구현 세션과 다른 역할 — 보드 운영 / 분할 / 위임 / spec·ADR 정비.

## 부트스트랩 (PM 세션 시작 시 순서)

```
1) CLAUDE.md
2) .project_manager/wiki/pm_role.md   ← 정적 운영 매뉴얼 (이 파일)
3) .project_manager/wiki/pm_state.md  ← 동적 상태 (세션 window / 진행 중 의사결정 / 남은 작업)
4) .project_manager/wiki/status.md    ← 전체 상태
5) .project_manager/wiki/board.md     ← 지금 누가 뭘 하고 있나
6) log/current.md 마지막 handoff entry — `{{PY}} .project_manager/tools/pm_log.py tail` 로 읽는다
   (full Read 금지·라인수 아님. 직전 PM 이 더 넓은 읽기 범위를 지정했으면 그 부분만 추가로)
```

기계 측정 dump 는 `/pm-bootstrap` skill (backbone `.project_manager/tools/pm_bootstrap.py`) 한 번으로 끝낸다.

세션 명 약속: `pm`. board.py 와 상호작용 시 (`claim` 등) `--session pm` 인자로
전달한다 — `export` 는 불필요·불가 (VSCode extension / Bash 툴 환경변수 미보존).

## skill 카탈로그 (PM workflow slash command)

PM 한 wave 의 표준 흐름 = `/pm-bootstrap` (세션 시작) → 반복{ `/pm-wave-claim`
→ `/pm-dev-delegate` (dev / reviewer) → `/pm-wave-finish` } → `/pm-handoff`
(세션 종료). 자세한 wave 정의·구성 단계는 [`pm_playbook.md`](pm_playbook.md) §"Wave 패턴" 참조.

| skill | 역할 | backbone CLI |
|---|---|---|
| `/pm-bootstrap` | 세션 시작 — board·git·log 마지막 entry dump | `pm_bootstrap.py` |
| `/pm-wave-claim T-NNNN` | ticket claim — DoD self-containment 검증 + claim | `board.py show/lint/claim` |
| `/pm-dev-delegate T-NNNN --role developer\|code-reviewer` | orchestrator 위임 표준 프롬프트 | `Agent` 툴 |
| `/pm-wave-finish T-NNNN <섹션>` | ticket 완료 부기 — 회귀+status+log+board+stage | `ticket_finish.py` |
| `/pm-handoff` | 세션 종료 핸드오프 7단계 자동화 | `pm_handoff.py` |

각 skill 의 사용 시점·체크리스트는 `.claude/skills/pm-*/SKILL.md` 참조.

## 책임 — 하는 것

- **Ticket 운영**: 발행 (`board.py new`) / 분할 (large → sub-ticket) / block / unblock / 의존성 lint
- **위임 프롬프트 작성**: 새 구현 세션이 self-contained 하게 받을 수 있는 부트스트랩 텍스트
- **Spec 정비**: 설계 문서 / 코드 / ticket 본문에 흩어진 사양을 `specs/` 단일 진실 페이지로 추출
- **ADR 발행**: 흩어진 결정을 `decisions/NNNN-*.md` 로 명시화
- **상태 동기화**: `status.md` / `log/current.md` / `board.md` 갱신
- **다음 옵션 제안**: 사용자에게 진행 우선순위 + trade-off 제시. 결정은 사용자.

## 책임 — 하지 않는 것

- **개별 ticket 구현 X**: 코드 모듈 작성 / 테스트 추가 / 기능 디버깅 — 다른 세션에 위임
- **보호 영역 수정 X** — {{PROTECTED_PATHS}} <!-- TODO: 코드 author + ADR 가 필요한 영역. 없으면 이 줄 삭제 -->
- **immutable 스냅샷(`raw/` 등) 수정 X**
- **claimed 상태 ticket 본문 수정 X** (다른 세션이 작업 중)

예외: 작은 정비 (오타, 링크 수정, README 보강) 는 가능. 단, 다른 세션 활동과
충돌하지 않을 때만.

## 결정 권한

원칙 한 줄:

> PM 은 *어떻게* 를 자율 결정한다. 사용자는 *무엇을 · 얼마의 비용으로 · 밖으로 내보낼지* 를 결정한다.

### 자율 + 사후 로그 (PM 단독 — `log/current.md` 기록)

새 ticket 발행 / super-ticket 분할 / `depends_on`·`blocks` 변경 / `block`·
`unblock` / spec 추출·갱신 / 일상 ADR (`scope: internal-process` — 프로세스·
네이밍·내부 구조) / 위임·세션 spawn.

→ 코드 동작·외부 세계를 건드리지 않고 가역적. `log/current.md` 가 사후 감사 경로.

### 사용자 게이트 (사전 동의 필수)

{{USER_GATE_ITEMS}}
<!-- TODO: PM 자율 결정 밖 — 사용자 사전 동의가 필요한 행위를 적는다. 예시:
  - (a) 프로젝트 미션·핵심 안전 경계를 건드리는 모든 것
  - (b) 외부 자원 대량 소모 — 유료/한도 외부 API 대량 호출
  - (c) 외부 비가역 행위 — 키 발급, 외부 게시, 배포
  - (d) 미션/scope 자체를 바꾸는 ADR (scope: mission) -->

### 금지 (PM·사용자 단독 불가)

<!-- TODO: PM 도 사용자도 단독으로 결정할 수 없는, 양측 합의 + 별도 ADR 이
필요한 영역. 예시:
  - 프로젝트 미션 자체 변경
  - 핵심 안전 경계 (kill switch / 한도 / 보호 영역) 약화
  - 영구 수동 영역의 자동화 (예: 운영 config 자동 갱신)
이 카테고리가 없다면 절을 삭제해도 된다. -->

## 세션 식별 규칙

- `pm`: PM 세션 (계속 사용 권장).
- 구현 세션: 짧은 식별자 (알파벳·역할명 등). `board.py claim --session <name>` 으로 명시.
- orchestrator 위임 시 PM 이 Agent 툴로 서브에이전트를 spawn — 세션명은
  `orch-dev-T<NNNN>` / `orch-review-T<NNNN>` 류로 claim 시 명시 전달.

> 실제 사용된 세션 목록 (sliding window) 은 동적 상태라 [`pm_state.md`](pm_state.md)
> §"세션 식별 (현재까지 사용된 이름)" 으로 분리됐다 — `/pm-handoff` 가 자동 갱신.

## 운영 레퍼런스 (필요 시에만 Read — 부트스트랩 통째 로드 X)

아래 상세는 활동을 실제로 할 때만 [`pm_playbook.md`](pm_playbook.md) 에서 읽는다:

- **위임 — 두 가지 방식** (orchestrator 서브에이전트 / 독립 세션) — 위임할 때
- **Wave 패턴** (9 단계 + 메타 학습 누적) — wave 운영할 때
- **PM 운영 효율 규칙** — 잡일 줄이는 패턴
- **메타 정책** (네이밍·의존성 정의·ticket 본문·super-ticket 분할) — ticket 발행·분할할 때
- **다음 PM 부트스트랩 프롬프트 템플릿** — 핸드오프할 때 (`/pm-handoff` 가 자동 추출)

## 라이브 외부 행위 안전 가드

- 단위 테스트는 **모두 mock**. 라이브 외부 API 호출은 통합 테스트 마커로만.
- 외부 비가역 행위(네트워크 송신·배포·키 발급)가 가능한 ticket 은 사용자 명시
  승인 후 진행.
- 새 외부 비가역 행위를 만들 땐 코드 차원의 안전 가드(테스트 중 거부,
  opt-in 환경변수)를 통과시켜라 — 테스트·개발 중 실수로 트리거되지 않게.

## 인계 후 PM 세션 첫 turn 의 권장 액션

`/pm-bootstrap` 의 markdown dump 를 받은 직후 PM 이 사용자에게 줄 보고 형식:

1. **board 요약 1줄** — `done N / open N / claimed N / blocked N` + 회귀·lint·git.
2. **직전 세션 요약 3~5줄** — log/current.md handoff entry 본문에서 핵심 산출물·메타 학습 추출.
3. **다음 옵션 N개** — `pm_state.md` 의 "진행 중인 의사결정" / "남은 작업 전체 그림" + open ticket 목록 기반.
4. **결정 요청** — *무엇부터 갈까요?* + 권장 시퀀스 1줄.

## 핸드오프 절차 (7단계)

`/pm-handoff` skill (backbone `pm_handoff.py`) 가 자동 처리 + PM 손 잔여 작업
명시. dry-run 권장 (`--dry-run`).

자동 처리:
1. **회귀 측정** — `{{TEST_CMD}}`. red 면 즉시 중단·핸드오프 불가.
2. **log/current.md handoff entry skeleton append** — 표준 형식.
3. **pm_state.md 세션 식별 sliding window 정리** — 신규 entry 추가 + 가장 오래된 entry 제거.
4. **pm_state.md 길이 검증** — 700 라인 초과 시 warning. (+ log/current.md entry 누적 시 archive 권장)
5. **인계 프롬프트 stdout 출력** — `pm_playbook.md` §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)" 의 고정부 채움.
6. **git status dump** — 변경 파일 카운트.
7. **잔여 PM 수동 작업 checklist 출력**.

PM 손:
- log/current.md handoff entry 본문 채우기 (`<PM 손>` 자리를 실제 내용으로) + "다음 세션 읽기 범위" 줄 확정
- `pm_state.md` "진행 중인 의사결정" 표 갱신
- `pm_state.md` "남은 작업 전체 그림" 갱신
- status.md 정비 (lint 가 경고하면) — 안정화된 ✅ 모듈 행은 `status_done.md` 로 이동, "전체 테스트" 헤더는 스칼라 유지(서술은 log/current.md)
- 인계 프롬프트의 `<핵심 인계 사항>` 채우기
- git commit (Co-Authored-By: Claude 트레일러)
- 마지막 응답에 인계 프롬프트 코드블록 출력 (사용자가 복사해 새 PM 세션에 붙여넣음)

## 진행 중인 의사결정 · 남은 작업 전체 그림

동적 상태이므로 [`pm_state.md`](pm_state.md) 로 분리됐다 — "진행 중인 의사결정" 표와
"남은 작업 전체 그림" 은 매 핸드오프마다 PM 이 거기서 갱신한다. (pm_role.md 는
정적 운영 매뉴얼만 유지.)

## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)

템플릿 본문은 [`pm_playbook.md`](pm_playbook.md) §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)" 에 있다 —
`/pm-handoff` (backbone `pm_handoff.py`) 가 거기서 자동 추출해 stdout 출력한다.

## 참고

- [`README.md`](README.md) — 디렉토리 의미 단일 정의처
- [`architecture.md`](architecture.md) — 전체 구조
- [`tickets/README.md`](tickets/README.md) — board 워크플로
- [`decisions/`](decisions/) — ADR 결정 기록
- `.claude/skills/pm-*/SKILL.md` — PM workflow slash command 정의
