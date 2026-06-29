---
title: PM Playbook (activity reference)
created: {{DATE}}
updated: {{DATE}}
type: reference
---

# PM Playbook — 활동별 레퍼런스

> [`pm_role.md`](pm_role.md)(매 부트스트랩에 항상 로드하는 운영 계약·안전 경계)에서 분리한
> **활동별 상세 레퍼런스**. 부트스트랩 시 통째로 읽지 않는다 — 해당 활동(위임 / wave 운영 /
> ticket 발행·분할 / 핸드오프)을 실제로 할 때 그 절만 Read 한다.
>
> ⚙️ **엔진** (`pm_update` 자동 갱신). `{{PY}}`(·`{{DATE}}`)는 `local.conf`/런타임 값으로 이해(리터럴 유지).

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
- **본문 = dev 컨텍스트 방화벽.** 정밀한 본문(정확한 함수/라인·인터페이스·패턴 reference)은 dev 의
  *읽기 범위*를 좁혀 cold subagent 의 컨텍스트 truncation 을 막는다. 모호한 본문 → 광범위 grep → 폭증.
- **ticket 크기는 두 축: 노력 + 컨텍스트.** `estimate` 는 노력 축이다. 노력이 small 이라도 touches 에
  대형 파일이 있거나 이해에 광범위 읽기가 필요하면 *컨텍스트가 크다* — **분할하거나** 본문에 정확한
  함수/라인·패턴 reference 를 박아 pre-digest 한다 (큰 ticket 의 pre-digest 는 architect 위임 후보).

### 디렉토리 의미
[`README.md`](README.md) "디렉토리 의미" 절이 단일 정의처 — 여기서 복제하지 않는다.

### 참조 규약 (파일명-무관 — 모든 LLM PM)
- ADR·ticket·idea 참조는 **항상 ID-wikilink**: `[[ADR-NNNN]]` · `[[T-NNNN]]`(`[[T-PFX-NNN]]`) · `[[idea-NNNN]]`.
- **생파일명·슬러그·markdown 경로 링크 금지** — ✅ `[[ADR-0006]]` / ❌ `[adr](decisions/0006-opencode-adapter.md)` ·
  ❌ `[[0006-opencode-adapter]]`. 엔진이 **번호로 resolve**(슬러그 무관)하므로 슬러그 정확성은
  애초에 불필요하고, lint 가 검증하는 것도 ID-wikilink 다. (LLM 이 파일명을 환각해 부정확하게 적던
  문제의 구조적 방어 — 슬러그가 틀려도 ID 는 항상 맞는다.)
- **enforce**: `board.py lint` 가 구조화 디렉토리를 가리키는 슬러그/생파일명
  참조를 포착(`unstable-ref`=차단·`unstable-ref-advice`=권고)하고 `lint --gate` 가 pre-push 차단. 자유어휘
  일반·산문 언급은 불검사(오탐 0).
- **문서 예시는 `<placeholder>` 꺾쇠나 코드 span/fence 안에** — 예: `` `[x](decisions/<slug>.md)` ``. lint 가
  코드 영역·`<…>` 를 자연히 건너뛰어 *예시 링크가 게이트를 막지 않는다*.

### status.md 정비 (부트스트랩 컨텍스트 경계)
- status.md 는 **judgment-only**(ADR-0023): **활성**(🟡/⬜/🔒) 모듈 *판정*(상태·비고) + 외부 의존성만.
  **테스트 수는 박제하지 않는다** — `board.py regression`(pytest) 실측이 단일 진실·history 는 log/current.md.
- ✅ 완성·안정 모듈 행은 `status_done.md` 로 옮긴다 (부트스트랩 컨텍스트 경계).
- 모듈 상태/비고 content-truth 는 **architect 가 유지·PM 점검**(ADR-0022/0023). incident/wave 서술은
  status 에 붙이지 않는다 — log/current.md entry 로.
- `board.py lint` 가 ✅ 누적(`status-done-accum`·>30행)을 warn 한다 (차단 아님 — `status_done.md` archive 권고).

### Super-ticket 분할 절차
1. 분할 결정 — **PM 자율**. `log/current.md` 에 분할 사유 기록 (과잉 분할 방지 규율).
2. 원본 ticket 을 `block --reason "Split into T-NNNN..T-MMMM"` 처리 (done 아님 — 작업 안 했으니).
3. sub-ticket 발행, 각 본문 self-contained 작성.
4. lint clean 확인 + 회귀 통과.
5. log/current.md 에 split entry append.

## 위임 — 두 가지 방식

ticket 본문이 self-contained 의무를 지므로 위임 프롬프트는 bespoke 일 필요 없다.

> **harness 노트:** 아래 위임 예시는 **claude(`Agent` 툴·`run_in_background`·`.claude/agents/`)** 기준이다.
> **opencode 는 네이티브 `task` 툴**(자식 세션)로 위임한다 — 도구·어휘·subagent 정의는 `AGENTS.md` §3·
> `.opencode/agents/` 를 본다. 위임 *원칙·절차*(축 분리·touches disjoint·single-source 프롬프트·산출 비준은
> PM)는 양 harness 동일 — 아래 본문은 그 원칙을 claude 어휘로 보일 뿐이다.

### 방식 A — orchestrator 서브에이전트 (Agent 툴, 권장)

PM 이 `Agent` 툴로 spawn 한다. `subagent_type` 으로 전용 정의를 쓴다 (세 축: 설계/구현/검토):

- **설계** — `subagent_type: architect` ([`.claude/agents/architect.md`](../../.claude/agents/architect.md), Opus). 설계 노동(idea 검토·ADR 초안·spec 추출·가설 검증·인터페이스). **산출은 PM 이 비준** — 발행·board·idea promote 는 PM.
- **구현** — `subagent_type: developer` ([`.claude/agents/developer.md`](../../.claude/agents/developer.md))
- **검토** — `subagent_type: code-reviewer` ([`.claude/agents/code-reviewer.md`](../../.claude/agents/code-reviewer.md))

세 정의가 역할·제약·부트스트랩·프로젝트 제약을 이미 담고 있으므로 PM 의 Agent
프롬프트는 한 줄이면 된다 (구현/검토는 `/pm-dev-delegate` skill 이 표준 프롬프트를 dump):

```
Idea-00NN 을 promote/kill 분석하고 promote 면 ADR 초안을 내라. (architect)
T-NNNN 을 구현하라. (developer)
T-NNNN 의 변경을 검토하라. 변경 파일: <경로>. (code-reviewer)
```

설계 spike 는 **PM 이 비준**한다 — architect 가 ADR/spec/idea-promote *초안·권고*를 내면, PM 이
검토 후 ADR 발행 / `board.py idea promote` / spec 승격 / log entry 를 한다 (generate≠evaluate 에 이은
"design labor ≠ decision"). 구현이 필요하면 PM 이 ticket 으로 발행해 developer 에 위임한다.

이 방식에서 **board.py claim/complete 와 status.md/log/current.md 갱신은
orchestrator(PM)가 한다** — 서브에이전트는 구현/검토만.

⚠️ code-reviewer 위임 프롬프트엔 "`status.md`/`log/current.md` 갱신은 orchestrator
담당 — 그 누락은 developer must-fix 아님" 을 덧붙인다 (reviewer 가 ticket DoD
의 status.md 항목을 developer 미이행으로 오판하는 것 방지).

**검토 루프:** dev → **내부 code-reviewer + codex external_review (둘 다)** →
must-fix 처리 (dev 재작업) → PM 회귀 verify → `board.py complete`. 이 루프 자체는
얇게 만들지 않는다 — 그 루프가 실전 결함을 잡는다.

git 도입 후 code-reviewer 는 `git diff` 로 변경 범위·내용을 직접 검증한다.

### codex 외부 교차검증 (표준 리뷰 게이트)

리뷰는 내부 서브에이전트 단독으로 끝내지 않는다 — **codex external_review 를
병행**한다. 근거: 내부 code-reviewer(generate≠evaluate)에 **외부 모델 다양성**을
더해 내부 맹점을 보완한다. (T-0001 실증 — 내부 reviewer 가 놓친 dest 뒤섞임
버그·path traversal 보안결함을 codex 가 포착, 전부 valid.) 전제:
`external_review_enabled=true` (local.conf·ADR-0004 opt-in — 비활성이면 `--dry-run`
미리보기·`--force` 1회 강제).

- **코드 리뷰** = 내부 code-reviewer + codex 외부 교차.
  ```
  {{PY}} .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN
  ```
  `--ticket` 이 touches 를 diff 경로로 잡고, `--adr` 이 관련 ADR 을 프롬프트에
  참조로 넣는다.
- **설계 리뷰** (ADR/spike) = codex 교차. ADR/spike 문서 자체를 diff 로 보낸다.
  ```
  {{PY}} .project_manager/tools/external_review.py --base <ref> --paths .project_manager/wiki/decisions/ ...
  ```
- **diff-only 한계 노트**: codex 는 **diff 만** 본다 (`--adr` 은 ID 참조일 뿐
  본문 미포함). ADR 본문을 봐야 정합을 판단할 수 있으면 — 특히 코드 ticket 이
  ADR 을 함께 개정할 때 — **`--paths` 에 코드 경로(ticket touches)와 ADR/문서
  경로를 함께 나열**한다. ⚠️ `--paths` 는 `--ticket` touches 를 *대체*하므로
  코드 경로를 빠뜨리면 코드 diff 가 리뷰에서 누락된다 — 또는 코드(`--ticket`)·
  설계(`--paths`)를 **별도 실행**한다 (T-0001 4차 교훈).
- 판정: codex 가 must-fix 감지 시 exit 1 (반려). 외부 호출 실패(인증/한도/
  네트워크/타임아웃) → exit 1 + `FALLBACK_INTERNAL` (내부 reviewer 폴백 신호).

### 방식 B — 독립 구현 세션 (별도 Claude 세션, 수동 spawn)

사용자가 다른 세션을 직접 열어 위임할 때. 그 세션이 board.py 까지 스스로 한다.
아래 고정 템플릿에 ticket ID·세션명만 끼운다:

```
당신은 이 프로젝트의 구현 세션 <X> 입니다. 역할: <T-NNNN> 단일 ticket 구현.
부트스트랩: 1) CLAUDE.md  2) .project_manager/wiki/status.md  3) {{PY}} .project_manager/tools/board.py show <T-NNNN>
작업 시작: {{PY}} .project_manager/tools/board.py claim <T-NNNN> --session session-<X>
ticket 본문의 목표 / 인터페이스 / 결정 / DoD 대로 수행.
완료 시: 전체 회귀 → board.py complete --tests-pass → status.md → log/current.md.
막히면 block --reason 으로 PM 세션에.
```

세션명은 `claim` 의 **`--session` 인자**로 전달한다 — `export` 가 아니다.

## Wave 패턴

**Wave** = 사용자 명시 *"wave 진행"* / *"최대한 많이 진행"* 명령에 PM 이
자율로 진행하는 작업 단위. 매 wave 사이 사용자 게이트 없이 다음 wave 로
이어진다 (사용자 신호 있을 때까지). 한 PM 세션 안에 보통 1~5 wave, 각 wave
는 1~여러 ticket 으로 구성. PM 자율 영역 (코드 동작·외부 세계 무영향·가역) 에
국한되며, 사용자 게이트 항목이 섞여 있으면 wave 중단·사용자 결정 대기.

### Wave 구성 (9 단계)

1. **ticket 발행** — PM 자율 (pm_role.md §"자율 + 사후 로그"). 본문은 self-contained
   의무 — 목표 / 인터페이스 / 결정 / DoD / 참고 / 메모. 신규 ticket 발행 비용 ↓
   만들수록 wave 효율 ↑.
2. **claim** — `/pm-wave-claim T-NNNN`. DoD self-containment·depends_on·
   placeholder·wikilink dangling 검증 후 claim.
3. **dev background 위임** — `/pm-dev-delegate T-NNNN --role developer`. Agent
   툴 `run_in_background: true`. **병렬 시 touches disjoint 필수** (file 겹침 0).
4. **(병렬 wave) dev 가 도는 동안 PM 의 안전한 작업** — touches 와 겹치지
   않는 다른 파일 편집·다른 ticket 본문 작성·`.project_manager/wiki/` 페이지
   정비 등. ⚠️ touches 겹치는 파일 편집 금지 (reviewer git diff 오염).
   ⚠️ 회귀 baseline 측정도 race 위험 — dev cycle 끝난 후 한 번에.
5. **reviewer 위임 + codex 교차** — `/pm-dev-delegate T-NNNN --role
   code-reviewer` (background) **+ codex external_review 병행** (위 §검토 루프
   "codex 외부 교차검증"). 내부 reviewer 위임 프롬프트엔 *"status.md /
   log/current.md 갱신은 orchestrator 담당 — 그 누락은 developer must-fix 아님"*
   명시. codex must-fix 는 내부 must-fix 와 합쳐 6단계에서 처리.
6. **PM should-fix 처리 분기** — reviewer 보고 후:
   - **PM 직접 fix**: 1줄·1패턴 변경 + dev 가 안 도는 영역. cycle 시간 절약.
   - **dev 재작업**: 여러 줄 변경 또는 dev 가 같은 file 작업 중.
   - **별도 ticket 후보 메모**: 본 ticket 범위 외 / 후속 caller 추가 시. *영구
     기록 = 다음 PM 세션이 결정 trail 추적 가능.*
   - **처리 보류 (suggestion)**: 운영 영향 0·기능 충분. 운영 영향·기능 충분
     여부가 should-fix vs suggestion 의 기준.
7. **ticket complete + 부기** — `/pm-wave-finish T-NNNN`
   (`ticket_finish.py` wrapper). 회귀 green 확인(red 면 중단·아무것도 안 건드림) →
   log/current.md 스켈레톤 append → board complete (`--tests-pass`) → git stage (`git add -A`).
   **status.md 는 더 이상 건드리지 않는다**(ADR-0023 — judgment-only · 테스트 수 박제 ✗).
   **모듈 행 판정/비고·git commit 은 PM 손**.
8. **PM 손 잔여** — log/current.md 서술 채우기 (스켈레톤 `<!-- PM: 무엇을·왜 -->` 를
   실제 내용으로) + status.md 모듈 행 판정/비고 (architect 유지·PM 점검 · 테스트 수는
   박제 안 함·ADR-0022/0023) + git commit (Co-Authored-By: Claude 트레일러).
9. **wave 종결 entry log/current.md append** — 패턴: `## [YYYY-MM-DD] complete | PM
   N차 wave M 종결 — <ticket 목록>`. 본문 = (a) 누적 변경 / (b) 회귀 delta /
   (c) **wave 메타 학습** (다음 wave·다음 PM 세션이 학습으로 사용) / (d) 보드
   상태 / (e) 다음 wave·다음 PM 세션 우선순위. wave 종결 commit 메시지에도
   wave 번호·ticket·핵심 메타 학습 요약 포함.

### Wave 메타 학습 누적

매 wave 의 *(c) 메타 학습* entry 가 다음 wave 의 의사결정에 영향. `log/current.md` 가
실측 학습 누적 매체 — 이 절은 정착 패턴만 흡수. 흔한 학습 카테고리:

- **dev 병렬도 안전 조건** — touches disjoint 가 기본 원칙. *공통 통합 파일에
  함수 단위 추가* 는 완화 조건 (서로 다른 함수면 git auto-merge OK).
- **reviewer 의 데이터·정합성 독립 검증** — 데이터/문서 ticket 은 reviewer
  fact-check 가 critical (dev spec 의 사실 오류 catch).
- **PM should-fix 직접 처리 trade-off** — cycle 시간 절약 vs dev 학습 누락.
  1줄·dev 안 도는 영역 기준.
- **reviewer 분석의 cross-check** — reviewer 도 항상 옳지 않다. PM 이
  should-fix 처리 전 *코드 흐름 자체* 독립 점검·부정확이면 변경 불필요 +
  log/current.md 영구 기록 (다음 PM 세션이 reviewer 평가 cross-check 신뢰도 활용).
- **ticket 본문 가설의 검증 책임 = PM 영역** — ticket 이 "X 가 silently wrong
  위험" 가설을 담으면 dev 는 그대로 받아 구현한다. 가설 자체의 도달 가능성
  검증은 PM 이 본문 작성 시 미리 한다 — (a) 가설 / (b) 코드 흐름에서 도달
  가능한 경로 / (c) fixture 가 그 경로를 재현, 3단계 명시.
- **dev↔reviewer 메모 통신** — dev 가 reviewer 평가 위임 메모 → reviewer
  분류 → PM 별도 ticket 후보 영구화. 3-actor 워크플로.

## PM 운영 효율 규칙

PM 병목은 "PM 이 한 세션"이 아니라 한 PM 이 직렬로 떠안는 잡일이다. PM 을
늘리지 않고(board·status·log·로드맵 단일 진실은 PM 1명) 잡일을 줄인다:

- **부기 자동화** — ticket 완료 부기(회귀 green → log/current.md 스켈레톤 → board complete
  → git stage)는 `.project_manager/tools/ticket_finish.py` / `/pm-wave-finish` skill 로 자동화
  (status.md 는 안 건드린다 — judgment-only·ADR-0023). PM 은 서술(왜·무엇)만 채운다. ⚠️ 단일 진실
  파일을 편집하므로 status.md **모듈 행 판정/비고**·**git commit** 은 자동화하지 않는다 — PM 손.
- **세션 시작·종료 자동화** — `/pm-bootstrap` (세션 시작 dump), `/pm-handoff`
  (세션 종료 7단계). PM 의 첫 turn / 마지막 turn 잡일을 한 명령으로.
- **dev→review 는 background 우선** — `Agent` 툴 `run_in_background: true` 로
  띄우고, 도는 동안 PM 은 *독립적인* 다음 ticket 을 설계한다. ⚠️ caveat:
  background 창에 PM 은 ticket 설계·`.project_manager/wiki/` 문서 작업만 한다 —
  검토 대상과 겹치는 코드 파일을 편집하면 reviewer 의 `git diff` 가 오염된다.
- **회귀 tmp 위생 (worktree 다발 실행 시 필수)** — orchestrator 가 worktree 에
  dev/reviewer 를 병렬로 띄우면 각 worktree 가 회귀를 반복 실행 → pytest run 번호
  폭증 + 기본 tmp 보존 정책이 *통과* run 까지 무한 누적된다 (실측: 한 어댑터에서
  /tmp 1.4TB 디스크 풀 인시던트·run 4882; autouse fixture 가 run 당 큰 tmp DB 를
  만들면 가속). **pytest 쓰는 인스턴스는 `pytest.ini` 에
  `tmp_path_retention_policy=failed` + `tmp_path_retention_count=3`** 을 둔다
  (통과 테스트 tmp 즉시 teardown·실패만 보존). `pytest.ini` 는 instance 소유라
  엔진이 자동 못 고침 — 채택 시 직접 추가. ⚠️ 중단 run 의 stale `.lock` 이
  옛 세션 cleanup 을 skip 하는 건 pytest+xdist 자체 동작(패치 불가) —
  `policy=failed` 가 디스크 영향을 무력화하는 게 실질 해법. **perf**: worktree
  다발 실행 시 `-n auto`(코어수) 워커가 경합하면 `-n N` 또는
  `PYTEST_XDIST_AUTO_NUM_WORKERS` 로 캡한다 (디스크는 retention 이 이미 해소).
- **ticket fact-gathering 위임** — ticket 본문의 *사실 수집*(파일 목록,
  cross-ref, grep)은 `Explore`/`general-purpose` 서브에이전트에 위임한다. 단
  본문의 **목표/결정/DoD 서술은 PM 이 직접** 쓴다 — self-containment 품질이
  거기서 나온다.
- **PM 은 적게 읽는다** — targeted read 우선(필요한 절만). 전체 파일 재read
  금지. 컨텍스트 수명이 늘어 인계 횟수가 준다.
- **사용자 첫 turn 결함 evidence = 우선순위 ↑·즉시 cycle** — 사용자가 PM
  세션 첫 turn 에 (a) 도구·skill·CLI 결함, (b) 테스트 인프라·CI 결함,
  (c) 부트스트랩 절차 결함 evidence 를 보고하면, *현 PM 세션·다음 PM 세션의
  cycle time 에 직접 영향* 인지 즉시 판단한다. **그렇다**: 직전 인계의 wave
  우선순위보다 위. ticket 발행 → PM 직접 또는 dev 위임 → (필요 시 reviewer)
  → commit 을 단일 turn cycle 로 처리. **그렇지 않다** (예: ticket 본문
  결함·spec drift·운영 evidence): wave 종료 후 idea 발행 또는 후속 ticket.
  근거: 부트스트랩·도구 결함 1 turn fix 의 ROI 가 wave 의 marginal dev 위임
  보다 크다 — 다음 세션도 같은 비용을 그대로 물려받기 때문.

## handoff 철학 (lean handoff · ADR-0008)

읽기 lean(위 §"PM 은 적게 읽는다")의 *쓰기* 짝. handoff entry 는 **파생 가능한 상태를
source 에 미룬다(defer-to-source)** — board/git/log/ADR 가 라이브로(또는 인접 entry 로)
답하는 것은 **재열거하지 않고 가리킨다(point, don't copy)**. 재열거는 중복이고, 중복은
stale 화로 거짓말한다 — board 는 거짓말하지 않는다.

**토큰-경제 근거 (왜):** *떠나는* 세션은 컨텍스트를 이미 들고 있어 압축이 거의 공짜(곧
버릴 것)지만, *들어오는* 세션이 raw durable state 에서 재유도하는 비용은 full 이고
append-only `log/current.md` 라 시간이 갈수록 커진다. → 떠나는 쪽이 싸게 압축한다.

**비파생 salient 레이어 (handoff 가 담는 것 — 떠나는 세션만이 싸게 줄 수 있고 ticket
상태에서 도출 불가 → 반드시 담는다):**

- **(a) 읽기 범위** — *포인터*("이 entry + T-NNNN~MMMM complete + 직전 PM N차 handoff")이지
  사본이 아니다. salience 는 board/log 에서 자동으로 안 나온다. (`pm_role.md` "full Read
  금지·라인수 아님" 과 정합 — 들어오는 세션이 포인터를 `pm_log.py tail`/targeted Read 로 따른다.)
- **(b) 메타 학습** — ticket 상태에서 도출 불가한 교훈만(산문). 드롭 시 영구 손실·같은 실수 재발.
- **(c) 다음 intent** — 두 줄로 세분(ADR-0008 재검토 트리거·T-0047). 대화에만 존재:
  - **대화 thread-tail** — 정지 직전 사용자 발화. ctx-trigger 경로는 어댑터 훅이 transcript 에서 추출해 자동 채운다(자동은 *초안* — 정지 후 PM 이 슬롯을 검토·편집한다. 민감 발화 노출 최소화). 대화형 경로는 PM 손.
  - **pending user intent** — 다음 우선순위 + 사용자 결정 대기. PM 손.

**FORBIDDEN (본문 재열거 금지 — 전부 source 가 답한다):**

- ❌ board done/open/claimed/blocked **카운트** (→ `board.py list`·`/pm-bootstrap` 라이브).
- ❌ **open ticket ID 목록** (→ `pm_bootstrap` 가 라이브로 출력).
- ❌ **commit 해시·push 상태** (→ `git log`/`git status`).
- ❌ **직전 complete entry 산출물 재요약** (→ 인접 entry. "읽기 범위" 로 가리켜라).

(금지는 *대량* 재열거에 한정 — board 상태·ticket 목록·commit·산문 재요약. board/git 은 즉시
파생이라 가리키면 된다.)

**회귀 숫자 규칙 (ADR-0008 Decision 5 · codex 2차 게이트 정합):** 회귀는 **1줄 baseline**
("N passed / 상태")이라 **green 도 항상 회귀/incident 라인에 적는다.** pm_bootstrap default 는
pytest skip 이고 "회귀: handoff entry 참조" 로 안내하므로, handoff 가 그 1줄을 들고 있어야
다음 세션이 기본 부트스트랩만으로 baseline 을 안다(생략하면 baseline 유실). 회귀 숫자만은
대량 재열거가 아니라 load-bearing 1줄이라 위 FORBIDDEN 에서 제외.

## 프레임워크 갱신 (pm_update)

upstream(프레임워크 reference)의 엔진 개선을 이 인스턴스로 당겨오는 **저빈도 유지보수 액션**.
**메인테이너가 업그레이드당 1회** 실행 → 커밋 → push. 나머지 팀원은 `git pull` (per-clone 아님 — 그건 `board.py init`).

1. upstream 체크아웃 확보 (reference repo 를 어딘가 clone/pull · v1 은 로컬 경로만).
2. `{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run` → 바뀔 엔진 파일 검토.
3. `--dry-run` 빼고 적용. 엔진 freshness 는 `local.conf` 의 `upstream_rev`↔`upstream_seen_rev`(git rev-baseline)로 추적된다(ADR-0036).
4. 엔진이 바뀌었으니 회귀 검증 — `{{PY}} .project_manager/tools/board.py regression run`.
5. 엔진 변경 커밋 + push (공유 — 팀원은 `git pull` 로 받음).

- 인스턴스 상태(board·status·log·tickets)·per-clone 로컬·커스터마이즈(`*.local.md`·루트 `CLAUDE.md`/`.gitignore`)는 **안 건드림**.
- 새 upstream 엔진 *파일* 을 받으려면 인스턴스 `engine.manifest` 에 그 경로를 추가한다 (manifest 는 인스턴스 소유).

## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)

핸드오프 절차 #5 에서 `/pm-handoff` 가 자동 출력 — 그대로 복사해 다음 PM 세션에 붙여넣는다.

> **트리거로 축소 (T-0180·ADR-0035·spike §3 옵션 C):** 인계 본문(읽기 범위·메타 학습·다음 intent·
> 회귀/incident)은 log handoff entry 가 단일 진실이고 **`/pm-bootstrap` 이 그 본문을 자동 dump**
> 한다(T-0179). 그래서 이 프롬프트는 *손-채움 인계 블록을 폐기*하고 역할 framing + `/pm-bootstrap`
> 트리거만 남긴다 — 같은 인계를 두 곳(프롬프트·log entry)에 적던 중복 제거.

```
당신은 이 프로젝트의 PM (Project Manager) 세션입니다.
역할: 보드 운영 / ticket 발행·분할 / 위임·orchestration / spec·ADR 정비.
개별 ticket 구현은 dev→review 서브에이전트에 위임하세요.

지금 /pm-bootstrap 을 실행하세요.
  → 차수 announce · board / git / log 라이브 dump · 직전 인계 본문(읽기 범위·메타 학습·
    다음 intent·회귀/incident) 자동 surface. 그 뒤 사용자에게 board 요약·직전 세션 요약·
    다음 옵션·결정 요청 4 단계로 보고하세요.
```

> **인계 본문은 log entry 가 carry:** 직전 PM 이 채운 lean handoff entry(읽기 범위·메타 학습·다음
> intent·회귀/incident)를 `/pm-bootstrap` 이 dump 한다 — 이 프롬프트에 손으로 옮겨 적지 않는다.
> *대화 thread-tail*(ctx-trigger 경로 어댑터 자동 초안)은 새 세션이 그 entry 슬롯에서 검토·편집한다.

---

> 프로젝트별 누적 학습·도메인 사례: [[pm_playbook.local]]
