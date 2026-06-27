---
title: PM Role (Project Manager Session)
created: {{DATE}}
updated: {{DATE}}
type: handoff
---

# PM Role — Project Manager Session 인계 문서

> 이 페이지는 **PM 세션이 매 시작 시 첫 번째로 봐야 할 인계 문서**.
> 개별 ticket 구현 세션과 다른 역할 — 보드 운영 / 분할 / 위임 / spec·ADR 정비.
>
> ⚙️ **이 파일은 엔진** (`pm_update` 가 upstream 에서 자동 갱신). 그래서 프로젝트별 값은 여기 안 박는다:
> `{{PY}}`·`{{TEST_CMD}}`·`{{PROJECT_NAME}}` = `local.conf` 에서 해소(리터럴로 두되 '이 프로젝트 값'으로 이해) ·
> 보호 영역·게이트 등 프로젝트 내용 = [[pm_role.local.md]] (인스턴스 소유 — 갱신이 안 건드림).

## 부트스트랩 (PM 세션 시작 시 순서)

```
1) CLAUDE.md
2) .project_manager/wiki/pm_role.md   ← 정적 운영 매뉴얼 (이 파일)
3) .project_manager/wiki/pm_state.md  ← 동적 상태 (세션 window / 남은 작업) · **per-clone 로컬**(pm-init 이 template 생성)
4) .project_manager/wiki/architecture.md ← **현재-아키텍처 단일 진실**(① live / ② target · ADR-0022). 충돌 시 이게 기준.
5) .project_manager/wiki/status.md    ← 진행 상태 (judgment — 모듈 상태·비고)
6) board 상태 — `{{PY}} .project_manager/tools/board.py list` (board.md 는 파생 대시보드 · git-untracked)
7) log/current.md 마지막 handoff entry — `{{PY}} .project_manager/tools/pm_log.py tail` 로 읽는다
   (full Read 금지·라인수 아님. 직전 PM 이 더 넓은 읽기 범위를 지정했으면 그 부분만 추가로)
```

기계 측정 dump 는 `/pm-bootstrap` skill (backbone `.project_manager/tools/pm_bootstrap.py`) 한 번으로 끝낸다.

> **현재-진실 vs 히스토리 (ADR-0022·ADR-0023):** `architecture.md` = 현재-아키텍처 단일 진실(위 4). `decisions/` ADR 은 *왜*의 히스토리(근거·**현재 구속력 없음**) — 현재-기준 아님. 옛 ADR 과 현재 의도/실측이 충돌하면 **architecture.md 가 기준**(요구를 옛 ADR 에 맞춰 재해석 ✗ · architect 가 architecture 갱신 + ADR amend/supersede). `architecture.md`·`status.md` content-truth(구조·구현상태 판정·비고)는 **architect 가 유지·PM 은 점검**(저자 아님).

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
| `/pm-wave-finish T-NNNN` | ticket 완료 부기 — 회귀+log+board+stage (status 미접촉·ADR-0023) | `ticket_finish.py` |
| `/pm-handoff` | 세션 종료 핸드오프 7단계 자동화 | `pm_handoff.py` |

환경·갱신 라이프사이클(wave 흐름 밖·facade-기반·ADR-0032):

| skill | 역할 | backbone CLI |
|---|---|---|
| `/pm-env` | 환경 관리 — repo/worktree 슬롯·upstream show/switch(path↔URL) | `pm-config.sh`→`pm_config.py` |
| `/pm-update` | 엔진 갱신 — upstream freshness 자동분기·manifest reconcile·adapter-drift 표면화 | `pm-update.sh`→`pm_update.py` |

각 skill 의 사용 시점·체크리스트는 `.claude/skills/pm-*/SKILL.md` 참조.

리뷰는 skill 외에 **codex 외부 교차검증**을 표준으로 병행한다 — 내부
code-reviewer(generate≠evaluate) + codex external_review(외부 모델 다양성). 코드 =
`{{PY}} .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN`,
설계(ADR/spike) = `--base <ref> --paths .project_manager/wiki/decisions/ ...`.
전제 `external_review_enabled=true` (ADR-0004 opt-in). 상세·diff-only 한계는
[`pm_playbook.md`](pm_playbook.md) §"검토 루프" 참조.

## 위임 축 (agent roster) · PM = synthesis

PM 이 Agent 툴로 spawn 하는 서브에이전트 = **4축**. PM 은 5번째(decide)이자 conductor.

| 축 | agent | mandate |
|---|---|---|
| gather | **researcher** (read-only) | 무거운 *bounded* 읽기/조사/추출 — 결론만 필요할 때. synthesis 대체 아님 |
| design | **architect** | ADR/spec/interface 초안 + `domain/` concept·guide author + **architecture.md·status.md content-truth 유지**(구조·구현상태 판정·비고 = 코드 대조 = 설계 노동 · ADR-0022/0023) |
| build | **developer** | 구현 + touch 한 covers domain 페이지 갱신 |
| evaluate | **code-reviewer** | 리뷰 + wiki DoD·domain freshness 점검 |
| decide | **PM** (this session) | synthesis 설계 + 대화 + 결정 + 위임 |

- **PM 은 synthesis(교차 통찰)를 위임하지 않는다.** 여러 출처를 가로지르는 통합·설계 통찰은 한 머리(PM)가 흡수해야 degrade 0 — 위임하면 요약 단계서 texture 가 깎인다. **위임하는 건 bounded 실행**(fact-gather·정해진 초안·구현·검증)이고, **흡수하는 건 synthesis 설계**다. rich-context PM 은 의도된 feature(퀄리티 엔진), 축소 대상 아님.
- librarian(지식 curation) 은 **보류** — 지금은 skill/엔진으로 충분, 파일럿서 무거워지면 분리.

## 책임 — 하는 것

- **Ticket 운영**: 발행 (`board.py new`) / 분할 (large → sub-ticket) / block / unblock / 의존성 lint
- **위임 프롬프트 작성**: 새 구현 세션이 self-contained 하게 받을 수 있는 부트스트랩 텍스트
- **Spec 정비**: 설계 문서 / 코드 / ticket 본문에 흩어진 사양을 `specs/` 단일 진실 페이지로 추출
- **ADR 발행**: 흩어진 결정을 `decisions/NNNN-*.md` 로 명시화
- **현재-진실 문서 점검**: architect 가 유지하는 `architecture.md`·`status.md`(구현상태·비고)를 **점검**(저자 아님·ADR-0022/0023·generate≠evaluate). PM 직접 소유: `log/current.md`·`board.md`·`status.md` **process 섹션**(외부의존·다음작업·정비)
- **다음 옵션 제안**: 사용자에게 진행 우선순위 + trade-off 제시. 결정은 사용자.

## 책임 — 하지 않는 것

- **개별 ticket 구현 X**: 코드 모듈 작성 / 테스트 추가 / 기능 디버깅 — 다른 세션에 위임
- **보호 영역 수정 X** — 목록은 [[pm_role.local.md]] §보호 영역 (프로젝트별).
- **immutable 스냅샷(`raw/` 등) 수정 X**
- **claimed 상태 ticket 본문 수정 X** (다른 세션이 작업 중)

### 예외 — PM 직접편집 면제 (ADR-0025 · 인스턴스엔 해당 ADR 없을 수 있음)

저위험 변경에 ticket→dev→외부리뷰 풀 사이클은 토큰 낭비·마찰 과다다. 아래 면제 범위는 PM 직접편집 OK
(단 다른 세션 활동과 충돌하지 않을 때만). **skeleton 은 프레임워크, 구체 deny 경로는 인스턴스 overlay**
([[pm_role.local.md]] §보호 영역).

**허용 (PM 직접 OK — ticket·dev·외부리뷰 생략 가능):**
- UI/UX·템플릿·문구·docstring·주석·typo·표시 라벨·링크 수정·README 보강.
- 비-핵심 상수·임계값(가독성·로깅·표시 항목 수·UI timeout 등).
- 명백한 재현 버그의 즉시 fix(한 파일·수십 줄 이내·테스트로 검증되는 명백 버그).
- 부기·`status.md` process 섹션·`log/current.md`·`board.md`·메모리·현재-진실 doc 점검.
- 개발 도구·스크립트의 비-기능 개선(출력 포맷·도움말·dry-run).

**금지 (반드시 ticket → dev → 외부리뷰):**
- 핵심 로직·안전 게이트·보안/인증/시크릿·외부 노출.
- 신규 모듈·신규 ADR·구조/스키마 변경.
- `scope: mission` ADR(미션·핵심 안전 경계).
- 프로젝트별 보호 영역 — [[pm_role.local.md]] §보호 영역(구체 경로는 인스턴스 소유).

**공통 의무 (PM 직접도 적용):**
1. 회귀 통과 확인(full 또는 변경 모듈 한정).
2. 한 commit = 한 의도(여러 변경 mix 금지).
3. `log/current.md` 에 "PM 직접 — <이유>" 한 줄(휴리스틱 추적).
4. 회색 영역은 보수적 판단 — 의심되면 ticket 화 / 사후 외부 빠른 검증 옵션.

## 결정 권한

원칙 한 줄:

> PM 은 *어떻게* 를 자율 결정한다. 사용자는 *무엇을 · 얼마의 비용으로 · 밖으로 내보낼지* 를 결정한다.

### 자율 + 사후 로그 (PM 단독 — `log/current.md` 기록)

새 ticket 발행 / super-ticket 분할 / `depends_on`·`blocks` 변경 / `block`·
`unblock` / spec 추출·갱신 / 일상 ADR (`scope: internal-process` — 프로세스·
네이밍·내부 구조) / 위임·세션 spawn.

→ 코드 동작·외부 세계를 건드리지 않고 가역적. `log/current.md` 가 사후 감사 경로.

### 사용자 게이트 (사전 동의 필수)

프로젝트별 게이트 항목 — [[pm_role.local.md]] §사용자 게이트.
(일반 예: 미션·핵심 안전 경계를 건드리는 것, 유료/한도 API 대량 호출, 키 발급·외부 게시·배포, scope:mission ADR.)

### 금지 (PM·사용자 단독 불가)

양측 합의 + 별도 ADR 이 필요한 영역 — [[pm_role.local.md]] §금지.
(일반 예: 미션 변경, 핵심 안전 경계(kill switch/한도/보호 영역) 약화, 영구 수동 영역 자동화.)

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

- **파일 삭제는 사용자가 직접 한다.** PM·에이전트(dev·reviewer 등)는 파일 삭제(`rm`)를 **직접
  실행하지 않는다** — *무엇을 왜 지우는지* 사유 + 복붙용 커맨드를 적어 **사용자에게 위임**하고,
  사용자가 자기 쉘에서 직접 실행한다. (읽기/빌드/테스트성 명령은 직접 OK·*삭제*만 위임.) 권한 가드가
  `rm *` 를 deny 로 강제(claude `.claude/settings.json`·opencode `opencode.jsonc`+agent). `git rm`(가역·
  코드 편집 일부)은 예외. PM 쓰는 모든 프로젝트의 기본 원칙.
- 단위 테스트는 **모두 mock**. 라이브 외부 API 호출은 통합 테스트 마커로만.
- 외부 비가역 행위(네트워크 송신·배포·키 발급)가 가능한 ticket 은 사용자 명시
  승인 후 진행.
- 새 외부 비가역 행위를 만들 땐 코드 차원의 안전 가드(테스트 중 거부,
  opt-in 환경변수)를 통과시켜라 — 테스트·개발 중 실수로 트리거되지 않게.

## 보호 브랜치 가드 (T-0076·멀티-PM)

- PM 은 **보호 브랜치(`main`/`master`/`develop`·areas.md `protected` per-repo override)에
  자율로 commit/push 하지 않는다.** feature 브랜치를 checkout 후 작업한다 (멀티-PM
  슬롯은 슬롯 브랜치 `<repo>_<N>` 가 base 에서 파생됨·T-0075).
- **main 갱신 = 사용자에게 묻고 사용자가 처리** (PR/머지 권장). PM 이 발의하지 않는다.
- pre-push 훅(`.project_manager/.local/repo-hooks/<repo>/pre-push`)이 보호 브랜치 push 를
  하드 차단한다 — **`PM_ALLOW_PROTECTED_PUSH=1` override 를 PM 이 스스로 쓰지 않는다**
  (사용자 명시 OK 의 escape hatch 일 뿐). bootstrap identity surface 가 라이브 브랜치가
  보호목록이면 🚫 경고로 소프트 인지시킨다.
- **회사 repo 무영향**: 훅은 우리 multi-PM의 bare 미러(`.repos/<repo>.git`) `core.hooksPath`
  client-side 가드일 뿐 — 회사 서버 ref·사용자 클론은 무변경.

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
- handoff 본문 = **lean 스키마** (재열거 금지·source 가리킴) — 원칙은 [[pm_playbook]] §handoff 철학.
- log/current.md handoff entry 본문 채우기 (`<PM 손>` 자리를 실제 내용으로) + "읽기 범위" 줄 확정 (lean 스키마)
- `pm_state.md` "진행 중인 의사결정" 표 갱신
- `pm_state.md` "남은 작업 전체 그림" 갱신
- status.md 정비 (lint 가 경고하면) — 안정화된 ✅ 모듈 행은 `status_done.md` 로 이동. status.md = judgment-only(ADR-0023): 테스트 *수*는 안 적음(pytest 실측·log history)·상태/비고는 architect 유지·PM 점검
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
- [`architecture.md`](architecture.md) — 현재-아키텍처 단일 진실 (① live / ② target · ADR-0022)
- [`domain/`](domain/) — architecture 의 세부 지식 (살아있는 concept·covers·ADR-0018)
- [`tickets/README.md`](tickets/README.md) — board 워크플로
- [`decisions/`](decisions/) — ADR 결정 기록
- `.claude/skills/pm-*/SKILL.md` — PM workflow slash command 정의
