---
title: PM Playbook (activity reference)
created: {{DATE}}
updated: {{DATE}}
type: reference
---

# PM Playbook — 활동별 레퍼런스

> [`pm_role.md`](pm_role.md)의 활동별 상세 레퍼런스. 부트스트랩 시 통째로 읽지 말고 해당 활동(위임 / wave 운영 / ticket 발행·분할 / 핸드오프) 절만 Read 한다.
>
> ⚙️ **엔진** (`pm_update` 자동 갱신). `python3` 표기는 관례(Windows 는 `py` 런처) · `{{DATE}}` 는 런타임 값으로 이해(리터럴 유지).

## 메타 정책 (코드/spec/ADR 어디에도 안 적힌 운영 약속)

### 네이밍
- 약어보다 풀네임. 의미를 정확히 담는 이름.

### 의존성 정의
- `depends_on` = **엄격한 코드 의존** (해당 ticket 산출물 없이 시작 불가). `board.py claim` 이 강제.
- `blocks` = **참조용 역방향 표기**. `A.blocks=[B]` 면 `B.depends_on` 에 `A` 반드시 있어야. `board.py lint` 가 강제.
- DI mock 가능하면 `depends_on` 에 넣지 않는다 (병렬 친화).

### Ticket 본문
- **self-contained 의무.** 새 세션이 본문만 보고 작업 시작 가능해야. template 만 채워 두면 안 됨.
- 표준 섹션(7절): 목표 / 인터페이스 / 결정 / 설계 / 완료 조건 / 참고 / 메모. `## 설계` 는 frontmatter `design: required` 인 ticket 만 채운다(`n/a`·`waived` 는 뼈대 유지·게이트 비대상).
- 참고: spec / ADR / 의존 모듈 / 패턴 reference (이미 done 된 비슷한 ticket).
- 본문에 정확한 함수/라인·인터페이스·패턴 reference 를 넣어 dev 읽기 범위와 cold subagent 컨텍스트를 제한한다.
- ticket 크기는 노력 + 컨텍스트 두 축이다. `estimate` 가 small 이어도 touches 에 대형 파일이 있거나 광범위 읽기가 필요하면 **분할하거나** 정확한 함수/라인·패턴 reference 로 pre-digest 한다. 큰 ticket pre-digest 는 architect 위임 후보.

### 디렉토리 의미
[`README.md`](README.md) "디렉토리 의미" 절이 단일 정의처 — 여기서 복제하지 않는다.

### 참조 규칙 (파일명-무관 — 모든 LLM PM)
- ADR·ticket·idea 참조는 **항상 ID-wikilink**: `[[ADR-NNNN]]` · `[[T-NNNN]]`(`[[T-PFX-NNN]]`) · `[[idea-NNNN]]`.
- **생파일명·슬러그·markdown 경로 링크 금지** — ❌ `[adr](decisions/0006-opencode-adapter.md)` · ❌ `[[0006-opencode-adapter]]`. 엔진이 번호로 resolve 하고 lint 도 ID-wikilink 를 검증한다.
- **enforce**: `board.py lint` 가 구조화 디렉토리를 가리키는 슬러그/생파일명 참조를 포착(`unstable-ref`=차단·`unstable-ref-advice`=권고)하고 `lint --gate` 가 pre-push 차단. 자유어휘 일반·산문 언급은 불검사.
- **문서 예시는 `<placeholder>` 꺾쇠나 코드 span/fence 안에** — 예: `` `[x](decisions/<slug>.md)` ``. lint 가 코드 영역·`<…>` 를 건너뛴다.

### status.md 정비 (부트스트랩 컨텍스트 경계)
- status.md 는 **judgment-only**: **활성**(🟡/⬜/🔒) 모듈 판정(상태·비고) + 외부 의존성만. 테스트 수는 박제하지 않는다 — `board.py regression`(pytest) 실측이 단일 진실·history 는 log/current.md.
- ✅ 완성·안정 모듈 행은 `status_done.md` 로 옮긴다.
- 모듈 상태/비고 content-truth 는 **architect 가 유지·PM 점검**. incident/wave 서술은 log/current.md entry 로.
- `board.py lint` 가 ✅ 누적(`status-done-accum`·>30행)을 warn 한다 (차단 아님 — `status_done.md` archive 권고).

### Super-ticket 분할 절차
1. 분할 결정 — **PM 자율**. `log/current.md` 에 분할 사유 기록 (과잉 분할 방지 규율).
2. 원본 ticket 을 `block --reason "Split into T-NNNN..T-MMMM"` 처리 (done 아님 — 작업 안 했으니).
3. sub-ticket 발행, 각 본문 self-contained 작성.
4. lint clean 확인 + 회귀 통과.
5. log/current.md 에 split entry append.

## 위임 — 두 가지 방식

ticket 본문이 self-contained 이므로 위임 프롬프트는 bespoke 일 필요 없다.

> **harness 노트:** 아래 예시는 **claude(`Agent` 툴·`run_in_background`·`.claude/agents/`)** 기준. **opencode 는 네이티브 `task` 툴**(자식 세션)로 위임한다 — `.opencode/pm-instructions.md`(위임 규약·`AGENTS.md` 공통 코어와 함께 자동 로드)·`.opencode/agents/` 참조. **codex 는 `.codex/agents/`(TOML 카드)** 를 쓰고 위임 채널은 하네스 네이티브다(`AGENTS.md` 공통 코어 + 하네스 운영 지침이 단일 진실). 역할 카드 경로는 하네스마다 다르므로(디렉토리·확장자 모두) 아래 목록은 카드를 `subagent_type` 이름으로만 가리킨다. 축 분리·touches disjoint·single-source 프롬프트·PM 산출 비준 원칙은 동일하다.

### 방식 A — orchestrator 서브에이전트 (Agent 툴, 권장)

PM 이 `Agent` 툴로 spawn 하고 `subagent_type` 으로 전용 정의를 쓴다:

- **설계(architect)** — `subagent_type: architect` (Opus). idea 검토·ADR 초안·spec 추출·가설 검증·인터페이스. **산출은 PM 이 비준** — 발행·board·idea promote 는 PM.
- **구현(developer)** — `subagent_type: developer`
- **검토(code-reviewer)** — `subagent_type: code-reviewer`

세 정의가 역할·제약·부트스트랩·프로젝트 제약을 담으므로 프롬프트는 한 줄이면 된다(구현/검토는 `/pm-dev-delegate` skill 이 표준 프롬프트를 dump):

```
Idea-00NN 을 promote/kill 분석하고 promote 면 ADR 초안을 내라. (architect)
T-NNNN 을 구현하라. (developer)
T-NNNN 의 변경을 검토하라. 변경 파일: <경로>. (code-reviewer)
```

설계 spike 는 PM 이 비준한다. architect 의 ADR/spec/idea-promote 초안·권고를 검토해 PM 이 ADR 발행 / `board.py idea promote` / spec 승격 / log entry 를 한다. 구현은 ticket 으로 발행해 developer 에 위임한다.

**board.py claim/complete 와 status.md/log/current.md 갱신은 orchestrator(PM)가 한다** — 서브에이전트는 구현/검토만.

⚠️ code-reviewer 프롬프트에 "`status.md`/`log/current.md` 갱신은 orchestrator 담당 — 그 누락은 developer must-fix 아님" 을 덧붙인다.

**검토 루프:** dev → **내부 code-reviewer + 추가 리뷰어 (둘 다)** → must-fix 처리(dev 재작업) → PM 회귀 verify → `board.py complete`. 루프를 생략하지 않는다. git 도입 후 code-reviewer 는 `git diff` 로 변경 범위·내용을 직접 검증한다.

### 추가 리뷰어 교차검증 (표준 리뷰 게이트)

내부 code-reviewer 와 **추가 리뷰어(additional reviewer)를 병행**한다. 역할 이름도 설정 키(`additional_reviewer_enabled`·`additional_reviewer.*`)도 추가 리뷰어로 통일돼 있다 — `external_review` 는 엔진 모듈 파일 이름·raw 파일 접두처럼 이미 기록된 산출물에 박힌 기계 식별자와 외부 전송 축의 이름으로만 남는다. 개칭 전 구키를 쓰는 채택자 `local.conf` 는 실행 시 안내 1줄을 받는다(마이그레이션 절차는 README).

전제는 `local.conf` 의 원자적 튜플 하나다(첫 init/update 에서 **1회만** 묻는다 — 비활성이면 `--dry-run` 미리보기·`--force` 1회 강제).

```
additional_reviewer_enabled=true
additional_reviewer.harness=codex
additional_reviewer.model=gpt-5.6-sol
additional_reviewer.reasoning=max
```

`additional_reviewer_enabled=true` 는 설정된 외부 전송과 통상 과금에 대한 **지속 동의**다 — 켠 뒤에는 리뷰마다·상한 재개마다 사용자에게 비용을 다시 묻지 않는다. 라운드/wave 상한은 비용 게이트가 아니라 기계적 anti-loop 정지이며 축마다 규율이 다르다:

- **리뷰 라운드 축(연장 승인 없음)** — 상한 2회(`review_rounds_max`), 직전 라운드 대비 must-fix 증가는 상한 전 조기 차단이다. rc=4 면 `--rounds-report` 로 장부를 읽고 **재설계·티켓 분할**로 전환한다(남은 지적은 다음 티켓 목표로 이동). 라운드를 연장하는 승인 플래그는 폐지됐고, 옛 플래그를 붙여 호출하면 rc=1 로 거부된다. 직전 지적의 해소 확인만 필요하면 게이트당 1회 `--confirm-fix`(확인 전용 라운드)를 쓰며, 거기서 나온 신규 발견은 재설계 신호로 본다.
- **wave 예산 축(재개 ack 유지)** — rc=4 면 `--rounds-report` 로 장부를 읽고 **같은 scope 의 정상 수렴이면 PM 이 자율로 `--ack-wave`** 하며 판단 근거를 log 에 남긴다. 예산을 열어도 라운드 축의 수렴 판정은 그대로 닫혀 있다.

**잔여 must-fix 의 처분(릴리즈 전 필수).** 상한으로 종결된 게이트에 must-fix 가 남았으면 그 잔여를 어떻게 소화했는지 장부에 선언한다. 건수를 읽지 못한 판정 무효 라운드의 잔여는 `0`이 아니라 **미상**이며 똑같이 차단·처분 대상이다. 선언 없는 잔여는 릴리즈가 열리지 않는다(`board.py livegate record` 가 실행 전에 차단·우회 플래그 없음). 보호훅의 `PM_SKIP_LIVE_GATE=1`도 장부 writer가 원자 갱신한 현행 잔여 표식이 명확히 `clear`일 때만 라이브 축을 우회한다. 표식 부재·손상·판독 실패는 잔여 미상이라 fail-closed이며, `board.py livegate record` 1회로 환경과 표식을 먼저 복구한다.

```bash
python3 .project_manager/tools/external_review.py --resolve-gate <게이트> --into <T-NNNN>    # 후속 티켓 재설계
python3 .project_manager/tools/external_review.py --resolve-gate <게이트> --fixed <근거 게이트>  # 코드로 해소
```

재설계(`--into`)는 면제가 아니라 처분이다 — 대상 티켓이 **done** 이어야 그 릴리즈가 열리므로 잔여는 같은 릴리즈 안에서 소화된다. 해소(`--fixed`)는 통과로 끝난 **근거 게이트**(확인 전용 라운드 또는 후속 게이트)를 지목하되, 근거 마지막 라운드가 차단 반려의 종료 **뒤에 시작**했고 실제 검토 diff의 `target_rev`가 반려 때와 달라야 한다. `ts`/`started_at`은 엄격한 ISO 8601 UTC여야 하며, 이 결속 필드가 없는 구 라운드·손상 시각은 “결속 불충분”으로 거부한다. 완료 시각만 늦은 동시 리뷰나 같은 미수정 diff의 통과는 근거가 아니다. 근거 게이트가 뒤이어 반려로 뒤집히면 릴리즈 시점 재검증에서 다시 막힌다. 선언은 그때의 라운드에 결속하므로 선언 뒤 새 반려 라운드가 오면 다시 선언해야 한다. `--resolve-gate`는 기록 명령이라 `--dry-run`과 함께 쓰면 부작용 없는 척 기록할 수 없어 rc=1로 거부한다(조회는 `--rounds-report`). 현재 처분 상태는 `--rounds-report` 의 처분 열(미처분/재설계→티켓/해소/무대상)로 확인한다. "사소하니 넘어간다"는 판단은 이 경로에 없다 — 판정 입력은 장부의 기록 사실뿐이다.

사용자에게 올리는 경우는 중대한 scope 확대·그 밖의 독립적 사용자 게이트 사유다.

레거시 `reviewer_cmd` 를 쓰던 채택자는 그대로 동작한다 — 엔진이 자동 마이그레이션하지 않고, 온보딩도 기존 결정을 덮지 않는다.

Claude Bash 도구로 아래 장시간 커맨드를 실행할 때는 호출층 `timeout: 29300000`(ms)을 반드시 명시한다. 엔진 CLI `--timeout`은 리뷰어 벽시계이고 Bash 호출층 timeout을 대신하지 않는다.

- **코드 리뷰** = 내부 code-reviewer + 추가 리뷰어 교차.
  ```
  python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN
  ```
  `--ticket` 이 touches 를 diff 경로로 잡고, `--adr` 이 관련 ADR 을 프롬프트에 참조로 넣는다.
- **설계 리뷰** (ADR/spike) = 추가 리뷰어 교차. ADR/spike 문서 자체를 diff 로 보낸다.
  ```
  python3 .project_manager/tools/external_review.py --base <ref> --paths .project_manager/wiki/decisions/ ...
  ```
- **diff-only 한계**: 추가 리뷰어는 **diff 만** 본다 (`--adr` 은 ID 참조일 뿐 본문 미포함). ADR 본문이 필요하거나 코드 ticket 이 ADR 을 함께 개정하면 **`--paths` 에 코드 경로(ticket touches)와 ADR/문서 경로를 함께 나열**한다. ⚠️ `--paths` 는 `--ticket` touches 를 *대체*하므로 코드 경로 누락 시 코드 diff 가 리뷰에서 빠진다. 또는 코드(`--ticket`)·설계(`--paths`)를 **별도 실행**한다.
- 판정: 추가 리뷰어가 must-fix 감지 시 exit 1 (반려). 외부 호출 실패(인증/한도/네트워크/타임아웃) → exit 1 + `FALLBACK_INTERNAL` (내부 reviewer 폴백 신호).

### 방식 B — 독립 구현 세션 (별도 Claude 세션, 수동 spawn)

사용자가 다른 세션을 직접 열어 위임할 때. 그 세션이 board.py 까지 스스로 한다. ticket ID·세션명만 넣는다:

```
당신은 이 프로젝트의 구현 세션 <X> 입니다. 역할: <T-NNNN> 단일 ticket 구현.
부트스트랩: 1) CLAUDE.md  2) .project_manager/wiki/status.md  3) python3 .project_manager/tools/board.py show <T-NNNN>
작업 시작: python3 .project_manager/tools/board.py claim <T-NNNN> --repo <repo> --slot <N>
ticket 본문의 목표 / 인터페이스 / 결정 / DoD 대로 수행.
완료 시: 전체 회귀 → board.py complete --tests-pass → status.md → log/current.md.
막히면 block --reason 으로 PM 세션에.
```

세션 정체성은 슬롯이면 `claim` 의 **`--repo <repo> --slot <N>` 인자**로, 커스텀 세션명이면 `$PM_SESSION_NAME` 환경변수로 준다.

## Wave 패턴

**Wave** = 사용자 명시 *"wave 진행"* / *"최대한 많이 진행"* 명령에 PM 이 자율 진행하는 단위. 사용자 신호까지 wave 사이 게이트 없이 계속한다. 한 PM 세션에 보통 1~5 wave, 각 wave 는 1~여러 ticket. 코드 동작·외부 세계 무영향·가역인 PM 자율 영역만; 사용자 게이트 항목이 섞이면 중단하고 결정을 기다린다.

### Wave 구성 (9 단계)

1. **ticket 발행** — PM 자율 (pm_role.md §"자율 + 사후 로그"). 본문은 self-contained: 목표 / 인터페이스 / 결정 / 설계(`design: required` 만) / DoD / 참고 / 메모.
2. **claim** — `/pm-wave-claim T-NNNN`. DoD self-containment·depends_on·placeholder·wikilink dangling 검증 후 claim. `design: required` ticket 은 설계 절 완성 + `design: done|waived` 승격 전까지 claim 이 rc=1 로 차단된다.
3. **dev background 위임** — `/pm-dev-delegate T-NNNN --role developer`. Agent 툴 `run_in_background: true`. **병렬 시 touches disjoint 필수** (file 겹침 0).
4. **(병렬 wave) dev 실행 중 PM 안전 작업** — touches 와 겹치지 않는 파일 편집·다른 ticket 본문 작성·`.project_manager/wiki/` 페이지 정비. ⚠️ touches 겹치는 파일 편집 금지(reviewer `git diff` 오염). ⚠️ 회귀 baseline 측정도 race 위험 — dev cycle 후 한 번에.
5. **reviewer 위임 + 추가 리뷰어 교차** — `/pm-dev-delegate T-NNNN --role code-reviewer` (background) **+ 추가 리뷰어 병행**. 내부 reviewer 프롬프트에 *"status.md / log/current.md 갱신은 orchestrator 담당 — 그 누락은 developer must-fix 아님"* 명시. 추가 리뷰어 must-fix 와 내부 must-fix 를 합쳐 6단계에서 처리.
6. **PM should-fix 분기**:
   - **PM 직접 fix**: 1줄·1패턴 변경 + dev 가 안 도는 영역.
   - **dev 재작업**: 여러 줄 변경 또는 dev 가 같은 file 작업 중.
   - **별도 ticket 후보 메모**: 본 ticket 범위 외 / 후속 caller 추가 시. 다음 PM 세션용 영구 기록.
   - **처리 보류 (suggestion)**: 운영 영향 0·기능 충분. 이것이 should-fix vs suggestion 기준.
7. **ticket complete + 부기** — `/pm-wave-finish T-NNNN` (`ticket_finish.py` wrapper). 회귀 green 확인(red 면 중단·아무것도 안 건드림) → log/current.md 스켈레톤 append → board complete (`--tests-pass`) → git stage — **그 ticket 이 선언한 경로만**.
   선언원 = frontmatter `touches` ∪ **이 실행이 실제로 쓴 산출물**, 즉 `log/current.md` + legacy 형상(board 미분리·출하 기본)에서 옮긴 티켓 파일의 **옛/새 경로** 둘뿐. ADR·domain 페이지·`architecture.md`·`status.md` 는 다른 실행 산출이므로 제외한다.
   stage 후 두 방향으로 loud 보고: `미스테이지 잔여`(내 누락이면 `touches` 보강 후 재stage·남의 WIP 면 그대로) · `스코프 밖 staged`(남이 올려둔 것 — bare commit 이면 실림·빼려면 `git restore --staged <경로>`).
   **status.md 는 건드리지 않는다**(judgment-only · 테스트 수 박제 ✗). **모듈 행 판정/비고·git commit 은 PM 손**(commit 도 pathspec 명시).
8. **PM 손 잔여** — log/current.md 스켈레톤 `<!-- PM: 무엇을·왜 -->` 를 실제 서술로 교체 + status.md 모듈 행 판정/비고(architect 유지·PM 점검, 테스트 수 박제 안 함) + **git commit — pathspec 명시**. bare `git commit` 은 무관한 staged 변경도 싣는다. `[4/5]` stage 경로 목록이 커밋 pathspec:
   ```
   git commit -m "T-NNNN — <title 요약>" -- \
     <ticket touches 의 실경로들> \
     .project_manager/wiki/log/current.md \
     .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
     .project_manager/wiki/tickets/done/T-NNNN-<slug>.md \
     .project_manager/wiki/status.md      # 모듈 행을 손봤을 때만
   ```
   **티켓 파일 두 줄(claimed·done)은 legacy 형상에서 필수** — 옛/새 경로를 함께 줘야 `claimed→done` rename 이 커밋된다. 누락하면 HEAD 에 ticket 이 `claimed` 로 남고 rename 이 index 에 남아 다음 커밋에 딸려간다. board 분리 형상이면 board-git 이 기록하므로 두 줄이 없다.
   ADR·domain 페이지처럼 **새 파일**을 함께 실으면 `git add <경로>` 선행. 미추적 경로를 pathspec 에 주면 `pathspec … did not match` 로 커밋 전체가 rc=1.
   (Co-Authored-By: Claude 트레일러). wave 단위 단일 commit 은 각 ticket 목록의 **합집합을 나열**하며 pathspec 생략·`-A` 로 갈음하지 않는다.
9. **wave 종결 entry log/current.md append** — 패턴: `## [YYYY-MM-DD] complete | PM N차 wave M 종결 — <ticket 목록>`. 본문 = (a) 누적 변경 / (b) 회귀 delta / (c) **wave 메타 학습** / (d) 보드 상태 / (e) 다음 wave·다음 PM 세션 우선순위. wave 종결 commit 메시지에도 wave 번호·ticket·핵심 메타 학습 요약 포함.

### Wave 메타 학습 누적

매 wave 의 *(c) 메타 학습*이 다음 wave 판단에 영향을 준다. `log/current.md` 가 실측 학습 누적 매체이며 이 절은 정착 패턴만 흡수한다:

- **dev 병렬도 안전 조건** — touches disjoint 기본. 공통 통합 파일에 서로 다른 함수 단위 추가는 git auto-merge 가능한 완화 조건.
- **reviewer 의 데이터·정합성 독립 검증** — 데이터/문서 ticket 은 reviewer fact-check 가 critical.
- **PM should-fix 직접 처리 trade-off** — cycle 시간 절약 vs dev 학습 누락. 1줄·dev 안 도는 영역 기준.
- **reviewer 분석 cross-check** — PM 이 should-fix 전 코드 흐름을 독립 점검. 부정확하면 변경 불필요 + log/current.md 영구 기록.
- **ticket 본문 가설 검증 = PM** — "X 가 silently wrong 위험" 같은 가설은 PM 이 본문 작성 시 (a) 가설 / (b) 코드 흐름상 도달 경로 / (c) fixture 재현을 명시해 검증한다.
- **dev↔reviewer 메모 통신** — dev 의 reviewer 평가 위임 메모 → reviewer 분류 → PM 별도 ticket 후보 영구화.

## PM 운영 효율 규칙

board·status·log·로드맵 단일 진실은 PM 1명이 유지하되 잡일을 줄인다:

- **부기 자동화** — ticket 완료 부기(회귀 green → log/current.md 스켈레톤 → board complete → git stage)는 `.project_manager/tools/ticket_finish.py` / `/pm-wave-finish` skill 로 자동화. status.md 는 안 건드린다. PM 은 서술(왜·무엇)만 채운다. ⚠️ status.md **모듈 행 판정/비고**·**git commit** 은 자동화하지 않는다 — PM 손. commit 도 pathspec 명시: `-- <touches> log/current.md [status.md]`.
- **세션 시작·종료 자동화** — `/pm-bootstrap` (세션 시작 dump), `/pm-handoff` (세션 종료 7단계).
- **dev→review 는 background 우선** — `Agent` 툴 `run_in_background: true`. 실행 중 PM 은 독립적인 다음 ticket 을 설계한다. ⚠️ background 창에는 ticket 설계·`.project_manager/wiki/` 문서 작업만; 검토 대상 코드 파일 편집 시 reviewer `git diff` 오염.
- **회귀 tmp 위생 (worktree 다발 실행 시 필수)** — worktree 병렬 회귀는 pytest run·tmp 를 폭증시킨다. **pytest 쓰는 인스턴스는 `pytest.ini` 에 `tmp_path_retention_policy=failed` + `tmp_path_retention_count=3`** 을 둔다(통과 tmp 즉시 teardown·실패만 보존). `pytest.ini` 는 instance 소유라 엔진이 자동 못 고치므로 채택 시 직접 추가. ⚠️ 중단 run 의 stale `.lock` 이 옛 세션 cleanup 을 skip 하는 pytest+xdist 동작은 패치 불가 — `policy=failed` 로 디스크 영향을 무력화한다. **perf**: worktree 다발 실행에서 `-n auto`(코어수) 워커가 경합하면 `-n N` 또는 `PYTEST_XDIST_AUTO_NUM_WORKERS` 로 캡한다.
- **ticket fact-gathering 위임** — 파일 목록·cross-ref·grep은 `Explore`/`general-purpose` 서브에이전트에 위임. **목표/결정/DoD 서술은 PM 이 직접** 쓴다.
- **PM 은 적게 읽는다** — targeted read 우선. 전체 파일 재read 금지.
- **사용자 첫 turn 결함 evidence = 우선순위 ↑·즉시 cycle** — 첫 turn 에 (a) 도구·skill·CLI, (b) 테스트 인프라·CI, (c) 부트스트랩 절차 결함 evidence 가 오면 현·다음 PM 세션 cycle time 에 직접 영향인지 판단한다. **그렇다**: 인계 wave 우선순위보다 앞세워 ticket 발행 → PM 직접 또는 dev 위임 → (필요 시 reviewer) → commit 을 단일 turn cycle 로 처리. **그렇지 않다**(ticket 본문 결함·spec drift·운영 evidence): wave 종료 후 idea 또는 후속 ticket.

## handoff 철학 (lean handoff)

handoff 는 board/git/log/ADR 에서 파생 가능한 상태를 재열거하지 않고 source 를 가리킨다.

**비파생 salient 레이어와 자동 박제(반드시 포함):**

- **(a) 이 세션 박제 entries** — `/pm-handoff`가 직전 자기 handoff 뒤의 complete/checkpoint 헤더를 자동 나열한다. 자기 경계가 없으면 직전 임의 handoff를 경계로 삼고, handoff가 전혀 없으면 `(경계 미해소 — 최근 10건)`과 함께 최근 10건만 싣는다.
- **(b) 메타 학습** — ticket 상태에서 도출 불가한 교훈.
- **(c) 다음 intent**:
  - **pending user intent** — 다음 우선순위 + 사용자 결정 대기. PM 손.

**FORBIDDEN (본문 재열거 금지):**

- ❌ board done/open/claimed/blocked **카운트** (→ `board.py list`[무인자=내 스트림·전체는 `--all`]·`/pm-bootstrap` 라이브).
- ❌ **open ticket ID 목록** (→ `pm_bootstrap` 가 라이브로 출력).
- ❌ **commit 해시·push 상태** (→ `git log`/`git status`).
- ❌ **직전 complete entry 산출물 재요약** (→ 자동 박제된 entry 헤더와 인접 원문을 따른다).

금지는 board 상태·ticket 목록·commit·산문을 대량 재열거하는 경우다.

**회귀 숫자 규칙:** 회귀는 **1줄 baseline**("N passed / 상태")으로 green 도 항상 회귀/incident 라인에 적는다. pm_bootstrap default 는 pytest skip 이고 "회귀: handoff entry 참조" 로 안내하므로 생략하면 baseline 이 유실된다. 회귀 숫자는 FORBIDDEN 예외다.

## 프레임워크 갱신 (pm_update)

upstream 엔진 개선을 당기는 저빈도 유지보수. **메인테이너가 업그레이드당 1회** 실행 → 커밋 → push. 팀원은 `git pull` (per-clone 은 `board.py init`).

1. upstream 체크아웃 확보 (reference repo 를 어딘가 clone/pull · v1 은 로컬 경로만).
2. `python3 .project_manager/tools/pm_update.py --from <upstream> --dry-run` → 바뀔 엔진 파일 검토.
3. `--dry-run` 빼고 적용. 엔진 freshness 는 `local.conf` 의 `upstream_rev`↔`upstream_seen_rev`(git rev-baseline)로 추적된다.
4. 엔진이 바뀌었으니 회귀 검증 — `python3 .project_manager/tools/board.py regression run`.
5. 엔진 변경 커밋 + push (공유 — 팀원은 `git pull` 로 받음).

- 인스턴스 상태(board·status·log·tickets)·per-clone 로컬·커스터마이즈(`*.local.md`·루트 `CLAUDE.md`/`.gitignore`)는 **안 건드림**.
- 새 upstream 엔진 *파일* 을 받으려면 인스턴스 `engine.manifest` 에 그 경로를 추가한다 (manifest 는 인스턴스 소유).

## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)

핸드오프 절차 #5 에서 `/pm-handoff` 가 자동 출력한다. 새 PM 세션 첫 메시지로 이 커맨드만 복사·붙여넣는다. 역할·인계는 CLAUDE.md·`/pm-bootstrap` 이 자동 로드/dump 한다.

```
/pm-bootstrap
```

인계 본문은 log handoff entry 가 단일 진실이다. `/pm-bootstrap` 이 자동 박제 entries·메타 학습·pending intent·회귀/incident를 dump 하므로 프롬프트에 옮겨 적지 않는다.

---

> 프로젝트별 누적 학습·도메인 사례: [[pm_playbook.local]]
