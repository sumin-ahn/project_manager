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
2) project_wiki/pm_role.md  ← 이 파일
3) project_wiki/status.md   ← 전체 상태
4) project_wiki/board.md    ← 지금 누가 뭘 하고 있나
5) project_wiki/log.md 마지막 ~30 라인
```

세션 명 약속: `pm`. board.py 와 상호작용 시 (`claim` 등) `--session pm` 인자로
전달한다 — `export` 는 불필요·불가 (VSCode extension / Bash 툴 환경변수 미보존).

## 책임 — 하는 것

- **Ticket 운영**: 발행 (`board.py new`) / 분할 (large → sub-ticket) / block / unblock / 의존성 lint
- **위임 프롬프트 작성**: 새 구현 세션이 self-contained 하게 받을 수 있는 부트스트랩 텍스트
- **Spec 정비**: 설계 문서 / 코드 / ticket 본문에 흩어진 사양을 `specs/` 단일 진실 페이지로 추출
- **ADR 발행**: 흩어진 결정을 `decisions/NNNN-*.md` 로 명시화
- **상태 동기화**: `status.md` / `log.md` / `board.md` 갱신
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

### 자율 + 사후 로그 (PM 단독 — `log.md` 기록)

새 ticket 발행 / super-ticket 분할 / `depends_on`·`blocks` 변경 / `block`·
`unblock` / spec 추출·갱신 / 일상 ADR (`scope: internal-process` — 프로세스·
네이밍·내부 구조) / 위임·세션 spawn.

→ 코드 동작·외부 세계를 건드리지 않고 가역적. `log.md` 가 사후 감사 경로.

### 사용자 게이트 (사전 동의 필수)

{{USER_GATE_ITEMS}}
<!-- TODO: PM 자율 결정 밖 — 사용자 사전 동의가 필요한 행위를 적는다. 예시:
  - (a) 프로젝트 미션·핵심 안전 경계를 건드리는 모든 것
  - (b) 외부 자원 대량 소모 — 유료/한도 외부 API 대량 호출
  - (c) 외부 비가역 행위 — 키 발급, 외부 게시, 배포
  - (d) 미션/scope 자체를 바꾸는 ADR (scope: mission) -->

## 메타 정책 (코드/spec/ADR 어디에도 안 적힌 운영 약속)

### 네이밍
- 약어보다 풀네임. 의미를 정확히 담는 이름.

### 의존성 정의
- `depends_on` = **엄격한 코드 의존** (해당 ticket 산출물 없이 시작 불가). `board.py claim` 이 강제.
- `blocks` = **참조용 역방향 표기**. `A.blocks=[B]` 면 `B.depends_on` 에 `A` 반드시 있어야. `board.py lint` 가 강제.
- DI mock 가능하면 `depends_on` 에 넣지 않는다 (병렬 친화).

### Ticket 본문
- **self-contained 의무.** 새 세션이 본문만 보고 작업 시작 가능해야. template 만 채워 두면 안 됨.
- 본문 표준 섹션: 목표 / 인터페이스 / 결정 / 완료 조건 / 참고 / 메모.
- 참고 섹션은 spec / ADR / 의존 모듈 / 패턴 reference (이미 done 된 비슷한 ticket) 포함.

### 디렉토리 의미
[`README.md`](README.md) "디렉토리 의미" 절이 단일 정의처 — 여기서 복제하지 않는다.

### Super-ticket 분할 절차
1. 분할 결정 — **PM 자율**. `log.md` 에 분할 사유 기록 (과잉 분할 방지 규율).
2. 원본 ticket 을 `block --reason "Split into T-NNNN..T-MMMM"` 처리 (done 아님 — 작업 안 했으니).
3. sub-ticket 발행, 각 본문 self-contained 작성.
4. lint clean 확인 + 회귀 통과.
5. log.md 에 split entry append.

### 세션 식별
- `pm`: PM 세션 (계속 사용 권장).
- 구현 세션: 짧은 식별자 (알파벳·역할명 등). `board.py claim --session <name>` 으로 명시.
- orchestrator 위임 시 PM 이 Agent 툴로 서브에이전트를 spawn — 세션명은
  `orch-dev-T<NNNN>` / `orch-review-T<NNNN>` 류로 claim 시 명시 전달.

## 위임 — 두 가지 방식

ticket 본문이 self-contained 의무를 지므로 위임 프롬프트는 bespoke 일 필요 없다.

### 방식 A — orchestrator 서브에이전트 (Agent 툴, 권장)

PM 이 `Agent` 툴로 spawn 한다. `subagent_type` 으로 전용 정의를 쓴다:

- **구현** — `subagent_type: developer` ([`.claude/agents/developer.md`](../.claude/agents/developer.md))
- **검토** — `subagent_type: code-reviewer` ([`.claude/agents/code-reviewer.md`](../.claude/agents/code-reviewer.md))

두 정의가 역할·제약·부트스트랩·프로젝트 제약을 이미 담고 있으므로 PM 의 Agent
프롬프트는 한 줄이면 된다:

```
T-NNNN 을 구현하라. (developer)
T-NNNN 의 변경을 검토하라. 변경 파일: <경로>. (code-reviewer)
```

이 방식에서 **board.py claim/complete 와 status.md/log.md 갱신은
orchestrator(PM)가 한다** — 서브에이전트는 구현/검토만.

⚠️ code-reviewer 위임 프롬프트엔 "`status.md`/`log.md` 갱신은 orchestrator
담당 — 그 누락은 developer must-fix 아님" 을 덧붙인다 (reviewer 가 ticket DoD
의 status.md 항목을 developer 미이행으로 오판하는 것 방지).

**검토 루프:** dev → review → (must-fix 있으면 dev 재작업) → PM 회귀 verify →
`board.py complete`. 이 루프 자체는 얇게 만들지 않는다 — 그 루프가 실전 결함을
잡는다.

git 도입 후 code-reviewer 는 `git diff` 로 변경 범위·내용을 직접 검증한다.

### 방식 B — 독립 구현 세션 (별도 Claude 세션, 수동 spawn)

사용자가 다른 세션을 직접 열어 위임할 때. 그 세션이 board.py 까지 스스로 한다.
아래 고정 템플릿에 ticket ID·세션명만 끼운다:

```
당신은 {{PROJECT_NAME}} 프로젝트의 구현 세션 <X> 입니다. 역할: <T-NNNN> 단일 ticket 구현.
부트스트랩: 1) CLAUDE.md  2) project_wiki/status.md  3) {{PY}} tools/board.py show <T-NNNN>
작업 시작: {{PY}} tools/board.py claim <T-NNNN> --session session-<X>
ticket 본문의 목표 / 인터페이스 / 결정 / DoD 대로 수행.
완료 시: 전체 회귀 → board.py complete --tests-pass → status.md → log.md.
막히면 block --reason 으로 PM 세션에.
```

세션명은 `claim` 의 **`--session` 인자**로 전달한다 — `export` 가 아니다.

## PM 운영 효율 규칙

PM 병목은 "PM 이 한 세션"이 아니라 한 PM 이 직렬로 떠안는 잡일이다. PM 을
늘리지 않고(board·status·log·로드맵 단일 진실은 PM 1명) 잡일을 줄인다:

- **부기 자동화** — ticket 완료 부기(회귀 → status.md 스칼라 → log.md 스켈레톤
  → board complete → git stage)는 `tools/ticket_finish.py` 로 자동화한다. PM 은
  서술(왜·무엇)만 채운다. ⚠️ 단일 진실 파일을 편집하므로 status.md **모듈
  행**·**git commit** 은 자동화하지 않는다 — PM 손.
- **dev→review 는 background 우선** — `Agent` 툴 `run_in_background: true` 로
  띄우고, 도는 동안 PM 은 *독립적인* 다음 ticket 을 설계한다. ⚠️ caveat:
  background 창에 PM 은 ticket 설계·`project_wiki/` 문서 작업만 한다 — 검토
  대상과 겹치는 코드 파일을 편집하면 reviewer 의 `git diff` 가 오염된다.
- **ticket fact-gathering 위임** — ticket 본문의 *사실 수집*(파일 목록,
  cross-ref, grep)은 `Explore`/`general-purpose` 서브에이전트에 위임한다. 단
  본문의 **목표/결정/DoD 서술은 PM 이 직접** 쓴다 — self-containment 품질이
  거기서 나온다.
- **PM 은 적게 읽는다** — targeted read 우선(필요한 절만). 전체 파일 재read
  금지. 컨텍스트 수명이 늘어 인계 횟수가 준다.

## 라이브 외부 행위 안전 가드

- 단위 테스트는 **모두 mock**. 라이브 외부 API 호출은 통합 테스트 마커로만.
- 외부 비가역 행위(네트워크 송신·배포·키 발급)가 가능한 ticket 은 사용자 명시
  승인 후 진행.
- 새 외부 비가역 행위를 만들 땐 코드 차원의 안전 가드(테스트 중 거부,
  opt-in 환경변수)를 통과시켜라 — 테스트·개발 중 실수로 트리거되지 않게.

## 핸드오프 절차

PM 세션 종료 전에:
1. `log.md` 에 이번 PM 세션의 주요 결정 / 발행 / spec 추출 entry append (+ 마지막에 `handoff` entry).
2. 진행 중인 결정이 있으면 아래 "진행 중인 의사결정" 표 갱신.
3. **다음 PM 세션 부트스트랩 프롬프트 작성** — 아래 템플릿을 채워, 떠나는
   세션의 마지막 응답에 코드블록으로 출력한다 (사용자가 복사해 새 PM 세션에 붙여넣음).
4. 컨텍스트 부족 신호 (< 10%) 면 즉시 사용자에게 보고 + 인계.

## 진행 중인 의사결정

<!-- TODO: 현재 진행 중인 큰 결정·작업을 표로 추적. 핸드오프마다 갱신. -->

| 항목 | 상태 |
|---|---|
| (예시) board | done 0 / open 0 / claimed 0 / blocked 0 |

## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)

핸드오프 절차 3단계에서 쓴다. **고정부는 그대로 두고 `<...>` 만 현재 인계
상태로 채운다.** 채운 결과를 떠나는 세션의 마지막 응답에 코드블록으로 출력한다.

```
당신은 {{PROJECT_NAME}} 프로젝트의 PM (Project Manager) 세션입니다.
역할: 보드 운영 / ticket 발행·분할 / 위임·orchestration / spec·ADR 정비.
개별 ticket 구현은 dev→review 서브에이전트에 위임하세요.

부트스트랩 (이 순서로):
1) CLAUDE.md
2) project_wiki/pm_role.md   ← PM 인계 문서 (가장 중요)
3) project_wiki/status.md
4) project_wiki/board.md
5) project_wiki/log.md 마지막 ~<N> 라인

세션 식별: board.py 와 상호작용 시 (claim 등) --session pm 인자를 쓰세요.

첫 turn 에 사용자에게: board 요약 / 회귀·lint / git 상태 / 다음 옵션 / 결정 요청.

핵심 인계 사항:
<- 직전 세션의 주요 결과 / board 상태 / 진행 중 작업 / 다음 권장 작업 /
   주의할 incident·교훈 / 외부 대기 항목을 5~10개 불릿으로.>

지금 부트스트랩 시작하세요.
```

## 참고

- [`README.md`](README.md) — 디렉토리 의미 단일 정의처
- [`architecture.md`](architecture.md) — 전체 구조
- [`tickets/README.md`](tickets/README.md) — board 워크플로
- [`decisions/`](decisions/) — ADR 결정 기록
