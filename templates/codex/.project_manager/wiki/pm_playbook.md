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
- 표준 섹션(7절): 목표 / 인터페이스 / 결정 / 설계 / 완료 조건 / 참고 / 메모. `## 설계` 는 frontmatter `design: required` 인 ticket 만 채운다(`n/a` 는 뼈대 유지·게이트 비대상). 설계 면제 값은 없다 — `required | done | n/a` 세 형식뿐이다.
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
1. 분할 결정 — 사용자가 선택한 목표 안에서 **claim 전 자족성 확보에 한해 PM 자율**. 원 티켓을 대체·종결하고 목표를 늘리지 않으며 `log/current.md` 에 분할 사유를 기록한다. claim 뒤 finding은 분할 입력이 아니다.
2. 원본 ticket 을 `block --reason "Split into T-NNNN..T-MMMM"` 처리 (done 아님 — 작업 안 했으니).
3. sub-ticket 발행, 각 본문 self-contained 작성.
4. lint clean 확인 + 회귀 통과.
5. log/current.md 에 split entry append.

### 판단 원칙
규칙의 단일 진실은 [`pm_principles.md`](pm_principles.md)(출하층) + PM 홈 로컬층 `pm_principles.local.md`(PM 홈 `.project_manager/wiki/` · 미출하)다. 행동 직전 훅(claude `.claude/ctx_stop_hook.py` · codex `pm_orch_codex.py` · opencode `plugins/principle-recall.js`)이 매칭 항목을 그 호출의 context 로 주입한다 — 규칙 본문을 여기서 복제하지 않는다.

## 위임 — 두 가지 방식

위임 단위는 **묶음(클러스터)** 이다 — 설계·리뷰·fix 는 묶음당 1회, 개발만 티켓당 1회다. 티켓은 PM이
소유하는 명세 파일 하나와, 설계→구현→리뷰→fix 가 한 건씩 쌓이는 라운드 파일들로 이뤄지며 각 위임
시점에도 self-contained다. 그래서 위임 프롬프트를 bespoke하게 재작성하지 않는다.
`/pm-dev-delegate`는 `pm_delegate.py ticket prepare|harvest --cluster`로 slot
run-dir(`.project_manager/.local/delegate-ticket-copies/` 아래) 하나에 티켓별 라운드 파일 N개를
전달·회수한다(슬롯 없이 PM이 직접 채울 자리는 `board.py section-add`가 예약한다). 에이전트는 자기
라운드 파일만 쓰고 명세·이전 라운드는 읽기 전용으로 읽는다. board 상태 전이는 PM 몫이고, 커밋·재배치·
머지는 묶음 종결(`ticket_finish.py --cluster`)이 실행한다.

> **harness 노트:** 아래 예시는 **claude(`Agent` 툴·`run_in_background`·`.claude/agents/`)** 기준. **opencode 는 네이티브 `task` 툴**(자식 세션)로 위임한다 — `.opencode/pm-instructions.md`(위임 규약·`AGENTS.md` 공통 코어와 함께 자동 로드)·`.opencode/agents/` 참조. **codex 는 `.codex/agents/`(TOML 카드)** 를 쓰고 위임 채널은 하네스 네이티브다(`AGENTS.md` 공통 코어 + 하네스 운영 지침이 단일 진실). 역할 카드 경로는 하네스마다 다르므로(디렉토리·확장자 모두) 아래 목록은 카드를 `subagent_type` 이름으로만 가리킨다. 축 분리·touches disjoint·single-source 프롬프트·PM 산출 비준 원칙은 동일하다.

`local.conf`의 `delegate.<role>[.<tier>].{harness,model,reasoning}`은 native/cross 공통 위임
설정이다. target이 현재 PM 하네스면 native agent transport, 다르면
`pm_delegate.py` cross transport를 고른다. `delegate.enabled`는 위임 전체의 마스터 스위치
(기본 허용·채널 무관)이고, 끄면 `pm_delegate` 실행·`ticket prepare`·훅이 깔린 하네스의 역할
spawn이 함께 막힌다. Claude native 카드의 `model:` drift는 가드가 비차단 경고로 표면화하며
설정·카드를 자동 수정하지 않는다.

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

**검토 루프(묶음 1회):** developer N → code-reviewer 1회(묶음) → PM finding 판정과 승인 delta(§라운드
프로토콜 5~6항) → developer 1명이 fix → 기계 확인(같은 절 8항 · `pm_delegate.py rounds resolve
--cluster <C-이름> --pm-verified` — 확인 커맨드는 엔진이 실행한다) → `ticket_finish.py --cluster`. 추가 리뷰어는 기본 OFF 인 opt-in 채널이라 `additional_reviewer.enabled=true` 인
채택자만 이 루프에 병행한다(§추가 리뷰어 교차검증). reviewer 산출은 구현 명령이 아니라
증거·제안이다. reviewer는 PM이 accepted한 구현·설계 결함마다 fix가 바로 실행할 수정·테스트 계약을
남긴다. 계약이 불완전하면 티켓을 정지해 사용자에게 보고한다. 리뷰는 1회이고 루프를 다시 열지 않는다.
git 도입 후 code-reviewer는 `git diff`로 변경 범위·내용을 직접 검증한다. PM-direct는 이 루프 대신 PM 구현·self-review·범위 테스트를 거친다.

### 라운드 프로토콜 (내부 루프 비용 규율)

라운드당 비용(회귀 벽시계 × 에이전트 × 라운드·PM 재작성 토큰)과 라운드 수 자체를 다음 규율로 통제한다.

1. **단계별 테스트 계약.** architect가 developer 착수 전에 지정 회귀의 대상·명령·기대값·음성 사례를 확정하고, 최초 developer는 그 계약을 green으로 만들어야 종료한다. reviewer는 must-fix마다 추가 회귀 계약을 낸다. fix는 architect 계약과 reviewer 계약을 모두 다시 실행하고 **전체 회귀까지 green**이어야 종료한다.
2. **검증 근거 지정 의무.** 위임 프롬프트는 "무엇으로 재는지"를 명시한다 — 실제 git 이 만든 산출물, 설치 바이너리에서 추출한 fixture, fake runner 아닌 층의 동작 단언. cold dev 는 트리 기억이 없어 검증 근거를 지정하지 않으면 픽스처를 지어내고(조립 문자열·순환 단언·문자열만 검사), 그 결함이 라운드를 늘린다.
3. **클래스 전수 열거 의무.** dev 는 구현 전에 결함 클래스의 인스턴스를 진입점·플랫폼·실패 모드·호출 경로 축으로 전수 나열해 보고하고 전부 처리한다. 보고된 형상만 처리한 결과는 미완이다. 전수 열거가 불가능하면 그 사실과 열거 경계를 보고한다. 클래스는 해당 결함의 클래스에 한정하며 티켓 밖 기능으로 스코프를 확대하지 않는다. 완료 보고에는 열거한 인스턴스 목록과 각각의 처리를 포함하며, 목록이 없으면 PM 이 반려한다.
4. **역방향 확인 의무.** dev 는 고침이 반대 방향 실패를 만들지 않았는지 단언한다. 느슨함을 조인 fix 는 과결속을, 조임을 푼 fix 는 누락을, 차단을 추가한 fix 는 정상 사용 차단을 각각 확인한다.
5. **finding/disposition 장부.** 블록 스키마의 단일 진실은 엔진 파서 상수이며 엔진이 골격을 공급한다. reviewer는 안정 ID와 must-fix별 수정·테스트 계약을 실값으로 채우고, PM은 `review disposition-template --cluster`가 낸 골격에 전수 disposition을 남긴다. finding 0도 reviewer의 0건 선언 뒤 fix 자리로 진행한다.
6. **accepted-only delta + 세션 재사용.** `pm_delegate.py review delta --cluster <C-이름>` 출력만 fix 프롬프트에 붙인다. 미판정·decision-required·계약 누락은 fix 준비를 차단하고 티켓을 정지해 사용자에게 보고한다. fix는 직전 dev 세션에 재사용하는 것이 기본이며 accepted가 0이어도 마지막 테스트·종결 자리로 실행한다.
7. **고정 예산.** 라운드 수는 묶음 장부가 선언한다 — `architect 1 · developer_per_ticket 1 ·
   code-reviewer 1 · fix 1`. 라운드 예약이 예산 초과와 순서 밖 역할을 **예약 전에** 거부하며
   표면(`--cluster` · `--ticket`)에 따라 판정이 갈리지 않는다. 네 값은 모두 정확히 1이며 순서는
   `architect → developer → code-reviewer → developer(fix)`다. 단계 생략·반복·예산 변경은 거부하고
   현재 티켓을 정지해 사용자에게 보고한다. 라운드를 더 얹는 플래그는 없다.
   예산이 세는 것은 **예약**이다(산출 유무를 보지 않는다) — 기계 확인은 스폰이 없어 예산에 들어가지
   않는다.
8. **확인은 기계가 먼저.** fix harvest는 architect 필수 테스트, reviewer가 요구한 추가 회귀,
   프로젝트 전체 회귀를 엔진이 실행한다. 이후 `rounds resolve --cluster <C-이름> --pm-verified`가
   확인 관측을 명세에 기록하며 PM이 커맨드나 confirmation을 손으로 옮겨 적지 않는다. 하나라도
   실패하면 fix 회수를 거부하고 추가 사람 라운드를 열지 않는다. 재현 커맨드는
   메타문자 없는 단일 비파괴 명령이어야 하며, 그 밖이면 실행하지 않고 fix 라운드를 반려한다.
9. **작업 중단 사유 판정.** 유효 집합 3항목만 작업 중단 사유로 인정한다. 무효 집합 5항목으로 중단하면 규약 위반이다. 각 항목은 조건과 결론을 함께 판정한다.

   **유효 집합.**
   - **사용자 명시 지시**: 조건: 사용자가 세션 종료 또는 작업 중단을 명시해 지시한 경우. 결론: 작업 중단 가능.
   - **사용자 결정 게이트**: 조건: 보호 영역·mission scope·외부 비가역 행위에 사용자 결정이 필요한 게이트에 도달한 경우. 결론: 작업 중단 가능.
   - **기술적 불가**: 조건: 필요한 자원이 부재하거나 권한이 거부되어 작업을 수행할 수 없는 경우. 결론: 작업 중단 가능.

   **무효 집합.**
   - **컨텍스트 잔량**: 조건: 컨텍스트 잔량을 작업 범위나 중단 결정과 함께 관측한 상태. 결론: 이를 이유로 작업을 중단하면 규약 위반이다.
   - **라운드·wave 상한**: 조건: 라운드·wave 상한에 도달한 상태. 결론: 새 라운드나 board 쓰기는 중단하고 사용자에게 근거를 보고한다.
   - **티켓 미완**: 조건: 티켓이 아직 미완인 상태. 결론: 이를 이유로 작업을 중단하면 규약 위반이다.
   - **남은 작업량**: 조건: 남은 작업량이 많다고 평가한 상태. 결론: 이를 이유로 작업을 중단하면 규약 위반이다.
   - **세션 자기 판단**: 조건: 세션이 "정확한 상태만 남기겠다"고 자기 판단한 상태. 결론: 이를 이유로 작업을 중단하면 규약 위반이다.

   **미완 보고 판정.**
   - **다음 행동 명시**: 조건: 세션이 "여기까지"라고 알리면서 다음 행동을 명시하지 않은 미완 보고. 결론: 다음 행동 없는 미완 보고는 규약 위반이다.
   - **자기 수행 우선**: 조건: 세션이 직접 수행할 수 있는 다음 행동이 남은 상태. 결론: 수행 가능한 행동을 남긴 미완 보고는 규약 위반이다.
   - **상한 이후 보고**: 조건: 라운드·wave 상한 도달로 해당 루프가 정지한 상태. 결론: 현재 티켓 상태와 실패 근거를 사용자에게 보고한다.
   - **종료·축소 권한**: 조건: 세션 종료 또는 작업 축소를 결정하는 경우. 결론: 세션 종료·작업 축소는 사용자 지시로만 한다.

wave 중 묶음 종결은 `ticket_finish.py --cluster <C-이름> --no-pytest` + 지정 회귀 실측 근거가 표준이다(전량 검증은 릴리즈 절차 1단계가 담당).

10. **처방 밖 수정 금지·빈틈은 보고.** fix 라운드에서 고칠 수 있는 것은 `review delta` 가 낸 finding ID 와
    각 허용 수정 범위뿐이다. 그 제약과 빈틈 보고 형식은 delta 렌더러가 출력 끝에 함께 싣는다 — 카드·스킬·
    프롬프트에 같은 문장을 복제하지 않고, PM 은 렌더된 출력을 발췌하지 않고 그대로 fix 프롬프트에 붙인다.
    처방대로 따르면 다른 결함이 생기는 상호작용을 developer 가 발견하면 스스로 메우지 않고 라운드 파일에
    빈틈을 적고 종료한다. 엔진은 라운드를 더 열지 않고 PM이 현재 티켓의 실패 근거를 사용자에게 보고한다.

### 추가 리뷰어 교차검증 (opt-in 채널 · 기본 OFF)

추가 리뷰어(additional reviewer)는 기본 OFF 인 opt-in 채널이다. `additional_reviewer.enabled=true` 로 켠 채택자만 code-reviewer 라운드에 이 채널을 병행하며, 아래 규약은 켠 경우에 적용된다. 역할 이름도 설정 키(`additional_reviewer.enabled`·`additional_reviewer.*`)도 추가 리뷰어로 통일돼 있다 — `external_review` 는 엔진 모듈 파일 이름·raw 파일 접두처럼 이미 기록된 산출물에 박힌 기계 식별자와 외부 전송 축의 이름으로만 남는다. 개칭 전 구키를 쓰는 채택자 `local.conf` 는 실행 시 안내 1줄을 받는다(마이그레이션 절차는 README).

전제는 `local.conf` 의 원자적 튜플 하나다(첫 init/update 에서 **1회만** 묻는다 — 비활성이면 `--dry-run` 미리보기·`--force` 1회 강제).

```
additional_reviewer.enabled=true
additional_reviewer.harness=codex
additional_reviewer.model=gpt-5.6-sol
additional_reviewer.reasoning=max
```

`additional_reviewer.enabled=true` 는 설정된 외부 전송과 통상 과금에 대한 **지속 동의**다 — 켠 뒤에는 리뷰마다·상한 재개마다 사용자에게 비용을 다시 묻지 않는다. 라운드/wave 상한은 비용 게이트가 아니라 기계적 anti-loop 정지이며 축마다 규율이 다르다:

- **리뷰 라운드 축(연장 승인 없음)** — 상한 2회(`additional_reviewer.rounds_max`), 직전 라운드 대비 must-fix 증가는 상한 전 조기 차단이다. rc=4면 `--rounds-report`로 장부를 읽고 현재 티켓을 정지해 사용자에게 보고한다. 라운드를 연장하는 승인 플래그는 폐지됐다.
- **wave 예산 축(재개 ack 유지)** — rc=4 면 `--rounds-report` 로 장부를 읽고 **같은 scope 의 정상 수렴이면 PM 이 자율로 `--ack-wave`** 하며 판단 근거를 log 에 남긴다. 예산을 열어도 라운드 축의 수렴 판정은 그대로 닫혀 있다.

**잔여 must-fix 의 처분(릴리즈 전 필수).** 상한으로 종결된 게이트에 must-fix 가 남았으면 그 잔여를 어떻게 소화했는지 장부에 선언한다. 건수를 읽지 못한 판정 무효 라운드의 잔여는 `0`이 아니라 **미상**이며 똑같이 차단·처분 대상이다. 선언 없는 잔여는 릴리즈가 열리지 않는다(`board.py livegate record` 가 실행 전에 차단·우회 플래그 없음). 보호훅의 `PM_SKIP_LIVE_GATE=1`도 장부 writer가 원자 갱신한 현행 잔여 표식이 명확히 `clear`일 때만 라이브 축을 우회한다. 표식 부재·손상·판독 실패는 잔여 미상이라 fail-closed이며, `board.py livegate record` 1회로 환경과 표식을 먼저 복구한다.

```bash
python3 .project_manager/tools/external_review.py --resolve-gate <게이트> --pm-verified
```

처분은 현재 티켓 fix의 판정 표면과 기계 확인 증거를 재검증하는 `pm-verified` 하나다. 선언은 그때의 라운드에 결속하므로 선언 뒤 새 라운드가 오면 stale로 다시 막힌다. `--resolve-gate`는 기록 명령이라 `--dry-run`과 함께 쓰면 rc=1로 거부한다. 상태는 `--rounds-report`의 처분 열(미처분/pm-verified/무대상)로 확인한다.

사용자에게 올리는 경우는 중대한 scope 확대·그 밖의 독립적 사용자 게이트 사유다.

리뷰어 대상은 구조화 키(`additional_reviewer.harness`·`.model`·`.reasoning`)로만 지정한다 — 옛 `reviewer_cmd` 통짜 커맨드 경로는 없어졌고, 엔진이 채택자 conf 를 대신 고쳐 쓰지 않으므로 그 키가 남아 있으면 소비 지점에서 멈추고 교체를 지목한다.

Claude Bash 도구로 아래 장시간 커맨드를 실행할 때는 호출층 `timeout: 29300000`(ms)을 반드시 명시한다. 엔진 CLI `--timeout`은 리뷰어 벽시계이고 Bash 호출층 timeout을 대신하지 않는다.

- **코드 리뷰**: code-reviewer 라운드와 같은 시점에 교차검증을 돌린다.
  ```
  python3 .project_manager/tools/external_review.py --ticket T-NNNN --adr ADR-NNNN
  ```
  `--ticket` 이 touches 를 diff 경로로 잡고, `--adr` 이 관련 ADR 을 프롬프트에 참조로 넣는다.
- **설계 리뷰** (ADR/spike): ADR/spike 문서 자체를 diff 로 보낸다.
  ```
  python3 .project_manager/tools/external_review.py --base <ref> --paths .project_manager/wiki/decisions/ ... --gate <T-NNNN|ADR-NNNN>   # 실 전송은 --gate(또는 --ticket 유도)나 명시적 --no-gate 필수
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

### Wave 구성 (묶음 단계)

wave 하나 = 묶음 하나다. 단계 표·커맨드의 단일 진실은 `/pm-dev-delegate` §클러스터 단계 표이고,
여기서는 각 단계에서 **PM이 판단할 것**만 적는다.

1. **ticket 발행 + 티어 기록** — PM 자율 (pm_role.md §"자율 + 사후 로그"). 본문은 self-contained:
   목표 / 인터페이스 / 결정 / 설계(`design: required` 만) / DoD / 참고 / 메모. `board.py tier-signals`
   의 h1·h2·docs-only 보조 신호를 보고 PM이 아래 순서의 첫 매치를 확정해 `board.py tier`로 기록한다.
   - **PM-direct**: touches 실제 파일 ≤2, 동작 무변경 또는 red→green 테스트 확정, hard 신호 0,
     완료 전 범위 테스트. 위임·리뷰 없음.
   - **hard**: 도구 모듈 2+, 공용 코드, 파싱 규칙, 기존 동작 영향, 보안·시크릿·외부 송신·git 훅,
     board 상태 전이·lease·잠금·동시성 중 1개 이상. 상위 developer 프로필을 쓴다.
   - **normal**: 소거법. 애매하면 상향한다.
2. **묶음 선언 + claim** — `board.py cluster new <이름> --tickets <T-NNNN,T-NNNN> --spike <설계 문서 경로>`
   로 이번 wave 의 운영 단위·통합 브랜치·설계 문서를 결속한다. 그 다음 멤버마다 `/pm-wave-claim`이
   DoD self-containment·depends_on·placeholder·wikilink dangling을 검증한다. **묶음 판단은 PM 몫**이다 —
   엔진은 touches 겹침·가용 슬롯을 경고만 하고 자동으로 묶지 않는다. 겹침이 있어도 슬롯이 남아 있으면
   병렬로 나눌 수 있다.
3. **설계(묶음 1회)** — `ticket prepare --cluster --role architect` 로 `01-architect` 를 예약한다.
   설계 단일 진실은 묶음 설계 문서 하나이고 티켓별 라운드 파일에는 그 티켓의 경계 실측·보정이 들어간다.
   `design: required`는 이 라운드 회수 전 claim/promote 가 rc=1이다. 면제 값은 없다.
4. **구현(티켓당 1회)** — `ticket prepare --cluster --role developer` 로 `02-developer` 를 예약하고
   티켓마다 슬롯에서 돌린다. PM-direct는 PM이 직접 구현·self-review한다. **병렬 시 touches disjoint
   필수**(file 겹침 0).
5. **(병렬 실행 중) PM 안전 작업** — touches 와 겹치지 않는 파일 편집·다른 ticket 본문 작성·
   `.project_manager/wiki/` 페이지 정비. ⚠ touches 겹치는 파일 편집 금지(리뷰 입력 오염).
   ⚠ 회귀 baseline 측정도 race 위험 — dev cycle 후 한 번에.
6. **리뷰(묶음 1회) + 추가 리뷰어 교차** — `--role code-reviewer --cluster` 가 스냅샷·프롬프트·라운드
   자리를 만든다. PM이 넣는 것은 검토 중점 문단(`--focus`) 하나이며, 거기에 *"status.md /
   log/current.md 갱신은 PM 담당 — 그 누락은 developer must-fix 아님"* 을 명시한다. 추가 리뷰어는
   켠 채택자만 같은 시점에 병행한다(티켓별 채널). PM-direct는 이 단계를 생략한다.
7. **판정 분기(PM 몫)** — finding 은 증거·제안이며 명령이 아니다. 판정 골격에 전수 판정을 채우고
   accepted 만 delta 로 내보낸다.
   - **fix 라운드**: accepted finding 전부를 reviewer 수정·테스트 계약대로 해소한다.
   - **rejected/suggestion**: board 의무로 바꾸지 않고 현재 판정에서 닫는다.
   - **결정 필요**: 목표 확대가 필요하면 board를 쓰지 않고 사용자에게 선택을 요청한다.
8. **묶음 종결** — `/pm-wave-finish`(`ticket_finish.py --cluster`)가 기계 확인 → 게이트 처분 →
   티켓별 완료 기록 → 슬롯 커밋 → 재배치 → 머지 → 슬롯 반납 → board·포인터 커밋을 고정 순서로
   실행한다. 실패 지점에서 멈추고 재실행이 곧 재개다. **status.md 는 건드리지 않는다**(judgment-only ·
   테스트 수 박제 ✗).
9. **PM 손 잔여 + 종결 entry** — log/current.md 스켈레톤 `<!-- PM: 무엇을·왜 -->` 를 실제 서술로 교체 +
   status.md 모듈 행 판정/비고(architect 유지·PM 점검). 묶음 산출 밖 파일(ADR·domain 페이지·status.md)은
   종결 커밋에 실리지 않으므로 그 경로만 따로 `git add` 해 별도 커밋으로 싣는다. 이어서 종결 entry 를
   append 한다 — 패턴: `## [YYYY-MM-DD] complete | <묶음> 종결 — <ticket 목록>`. 본문 = (a) 누적 변경 /
   (b) 회귀 delta / (c) **메타 학습** / (d) 보드 상태 / (e) 다음 묶음·다음 PM 세션 우선순위.

### Wave 메타 학습 누적

매 wave 의 *(c) 메타 학습*이 다음 wave 판단에 영향을 준다. `log/current.md` 가 실측 학습 누적 매체이며 이 절은 정착 패턴만 흡수한다:

- **dev 병렬도 안전 조건** — touches disjoint 기본. 공통 통합 파일에 서로 다른 함수 단위 추가는 git auto-merge 가능한 완화 조건.
- **reviewer 의 데이터·정합성 독립 검증** — 데이터/문서 ticket 은 reviewer fact-check 가 critical.
- **PM should-fix 직접 처리 trade-off** — cycle 시간 절약 vs dev 학습 누락. 1줄·dev 안 도는 영역 기준.
- **reviewer 분석 cross-check** — PM 이 판정 전 코드 흐름을 독립 점검. 부정확하면 rejected 사유로 남기고 log/current.md 에 영구 기록.
- **ticket 본문 가설 검증 = PM** — "X 가 silently wrong 위험" 같은 가설은 PM 이 본문 작성 시 (a) 가설 / (b) 코드 흐름상 도달 경로 / (c) fixture 재현을 명시해 검증한다.
- **dev↔reviewer 메모 통신** — dev의 평가 메모를 reviewer가 finding/suggestion으로 분류하고 PM이 현재 티켓에서 판정한다.

## PM 운영 효율 규칙

board·status·log·로드맵 단일 진실은 PM 1명이 유지하되 잡일을 줄인다:

- **종결 자동화** — 묶음 종결(기계 확인 → 게이트 처분 → 티켓별 완료 기록 → 슬롯 커밋 → 재배치 → 머지 → 슬롯 반납 → board·포인터 커밋)은 `.project_manager/tools/ticket_finish.py --cluster` / `/pm-wave-finish` skill 이 고정 순서로 실행한다. **손 git 은 0**이고 커밋 문안도 엔진이 낸다. status.md 는 안 건드린다. PM 은 서술(왜·무엇)과 status.md **모듈 행 판정/비고**만 채우며, 묶음 산출 밖 파일(ADR·domain·status.md)은 그 경로만 따로 `git add` 해 별도 커밋으로 싣는다.
- **세션 시작·종료 자동화** — `/pm-bootstrap` (세션 시작 dump), `/pm-handoff` (세션 종료 7단계).
- **dev→review 는 background 우선** — 실행 중 PM은 검토 대상 코드와 board를 바꾸지 않고 현재 티켓의 읽기 전용 근거만 정리한다. 검토 대상 코드 파일 편집은 reviewer `git diff`를 오염시킨다.
- **회귀 tmp 위생 (worktree 다발 실행 시 필수)** — worktree 병렬 회귀는 pytest run·tmp 를 폭증시킨다. **pytest 쓰는 인스턴스는 `pytest.ini` 에 `tmp_path_retention_policy=failed` + `tmp_path_retention_count=3`** 을 둔다(통과 tmp 즉시 teardown·실패만 보존). `pytest.ini` 는 instance 소유라 엔진이 자동 못 고치므로 채택 시 직접 추가. ⚠️ 중단 run 의 stale `.lock` 이 옛 세션 cleanup 을 skip 하는 pytest+xdist 동작은 패치 불가 — `policy=failed` 로 디스크 영향을 무력화한다. **perf**: worktree 다발 실행에서 `-n auto`(코어수) 워커가 경합하면 `-n N` 또는 `PYTEST_XDIST_AUTO_NUM_WORKERS` 로 캡한다.
- **ticket fact-gathering 위임** — 파일 목록·cross-ref·grep은 `Explore`/`general-purpose` 서브에이전트에 위임. **목표/결정/DoD 서술은 PM 이 직접** 쓴다.
- **PM 은 적게 읽는다** — targeted read 우선. 전체 파일 재read 금지.
- **사용자 첫 turn 결함 evidence = 현재 범위에서 수렴** — 도구·skill·CLI, 테스트 인프라·CI, 부트스트랩 결함 evidence는 현재 사용자가 정한 범위에서 우선 처리하며, 새 board 의무나 목표 확대는 사용자 선택 전 만들지 않는다.

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
3. `--dry-run` 빼고 적용. 엔진 freshness 는 `local.conf` 의 `upstream.rev`↔`upstream.seen_rev`(git rev-baseline)로 추적된다.
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
