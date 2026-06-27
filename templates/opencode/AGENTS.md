# AGENTS.md — opencode PM 어댑터

> opencode 세션이 시작될 때 자동 로드되는 진입점. **이 파일이 PM(Project Manager) 운영의
> 자족적 매뉴얼이다.** Claude Code 비의존 — opencode LLM(로컬 gemma / 회사 Pro)이 이 문서만
> 읽고 스스로 PM 을 부트스트랩·운영한다.
>
> 대응 관계: 이 파일 = Claude Code 타깃의 `CLAUDE.md`. 단 **그대로 번역이 아니라**
> opencode 실행 모델(build primary + 네이티브 `task` tool 위임)에 맞게 재서술했다. (ADR-0006 · PM 9차)

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 0. opencode 실행 모델 (PM 멘탈 모델)

- **PM(orchestrator) = `pm` primary agent (1차) · build primary (폴백).**
  - **1차 = `pm` primary** (`.opencode/agents/pm.md` · `mode: primary`). relay(ADR-0009·세션 회전
    supervisor·ADR-0020 개명)가 PM 세션을 **deterministic 하게 spawn** 하는 타깃이다 — `opencode run --agent pm` 으로 올바른
    모델(Pro)·풀권한·안전 가드가 박힌 PM 세션이 뜬다. pm.md 본문은 thin — 이 문서로 부트스트랩하라고
    가리킨다.
  - **폴백 = build primary.** 회사판 opencode 가 custom primary(`mode: primary`)를 노출/허용하는지
    **미검증**(opencode-pm-adapter spike §6)이므로, `pm` 이 안 떠도 PM 부트스트랩이 안 깨지게 한다 —
    이 문서를 읽은 build 세션도 곧 PM 이다(plan/build 두 타입만 노출해도 무관). **PM 동작의 단일 진실은
    이 문서**이므로 어느 진입점이든 동일하게 PM 으로 구동된다. (additive — ADR-0006 amendment, 비준은 PM)
- **위임 = 네이티브 `task` tool.** PM 은 dev/reviewer/architect 역할을 내장 `task` tool 로
  위임한다 — opencode 가 `.opencode/agents/*.md` (mode: subagent) 를 **별도 자식 세션**에서
  구동한다 (fresh 컨텍스트 = 200K 격리 · 자식 model/권한이 subagent 정의대로 — PM 9차 실증).
  **폴백 = `opencode run` 외부 프로세스** (headless·CI·task tool 미노출 빌드). §3 위임 규약.
- **엔진 = 공유 python.** PM 운영 로직은 `.project_manager/tools/*.py` (board.py·pm_*.py)에
  있다. PM 은 bash tool 로 이 CLI 를 호출·해석한다. **엔진은 0 수정** — 어댑터(이 문서·
  `.opencode/`)만 타깃별로 다르다.
- **인코딩 = 엔진이 코드로 처리.** 엔진이 인코딩을 코드로 처리(PM 7차·C1 파일·C2 콘솔 reconfigure) —
  env prefix 불필요. Windows/CP949 환경서도 env 없이 한글 ticket·wiki 가 깨지지 않는다. §1.

## 1. 엔진 호출 규약 (인코딩)

엔진 python CLI 는 env prefix 없이 그대로 호출한다:

```bash
{{PY}} .project_manager/tools/board.py list
```

- 엔진이 인코딩을 코드로 처리(PM 7차·C1 파일 IO `encoding="utf-8"`·C2 콘솔 stdout reconfigure) —
  env prefix 불필요. Windows/CP949·PowerShell 환경서도 env 없이 한글 ticket·wiki 깨짐 0 으로 동작(실측).
- 구버전 Windows·서드파티 파이프서 드물게 필요하면 **각 셸 문법으로** 붙인다 —
  PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`. (bash 문법을 규약으로 강제하지 않는다.)

> 인터프리터: `{{PY}}` 는 setup 시 채택 환경의 인터프리터로 치환된다
> (`.project_manager/local.conf` 의 `py=` 가 단일 진실 — `board.py init` 이 설정 ·
> venv 면 `venv/bin/python`).

## 2. PM 부트스트랩 (세션 시작 시 순서)

build 세션이 시작되면 다음을 순서대로 수행한다. **Read tool 로 파일을 읽을 땐 절대 경로를 쓴다.**

1. **이 문서(AGENTS.md)** — 이미 로드됨. opencode 실행 모델·위임·인코딩 규약 파악.
2. **PM 운영 매뉴얼** — `.project_manager/wiki/pm_role.md` (정적 운영 매뉴얼: 책임·결정 권한·핸드오프).
3. **PM 동적 상태** — `.project_manager/wiki/pm_state.md` (세션 window·진행 중 의사결정·남은 작업).
   *없으면* 채택 setup 미완 — `board.py init` 이 template 에서 생성한다.
4. **현재-진실 + 진행 상태** — `.project_manager/wiki/architecture.md`(**현재-아키텍처 단일 진실**·① live / ② target · ADR-0022 · 부트스트랩 1순위·충돌 시 기준) → `.project_manager/wiki/status.md`(모듈 진행상태·비고). ADR(`decisions/`)은 *왜*의 히스토리(현재 구속력 없음).
5. **보드 조회** — 지금 잡을 수 있는 ticket 확인:
   ```bash
   {{PY}} .project_manager/tools/board.py list
   ```
6. **직전 세션 핸드오프** — log 의 마지막 entry 만 (full Read 금지):
   ```bash
   {{PY}} .project_manager/tools/pm_log.py tail
   ```

> **기계 측정 일괄 dump (선택).** `pm_bootstrap.py` 는 board·git·log 를 한 번에 dump 한다 —
> `{{PY}} .project_manager/tools/pm_bootstrap.py`. 또는 위 5·6 의
> board.py·pm_log.py 직접 호출로 대체한다 (둘 다 1급 경로).

### 세션 식별

- PM 세션명은 **`pm`** 고정. board.py 조작 시 `--session` 인자로 전달한다:
  ```bash
  {{PY}} .project_manager/tools/board.py claim T-NNNN --session pm
  ```
  (board.py 식별 우선순위: `--session` 인자 > `$PM_SESSION_NAME`[구 `$CLAUDE_SESSION_NAME` = deprecated alias] > local.conf `session=` > `hostname-pid`.)
- 위임(task subagent · 폴백 프로세스)의 식별 라벨 — `orch-dev-TNNNN` / `orch-review-TNNNN` (§3).

### 첫 turn 권장 보고 (부트스트랩 직후)

1. **board 1줄** — `done N / open N / claimed N / blocked N` + 회귀·lint·git 상태.
2. **직전 세션 요약 3~5줄** — log 마지막 handoff entry 에서 핵심 산출물·메타 학습.
3. **다음 옵션 N개** — pm_state.md "남은 작업" + open ticket 기반.
4. **결정 요청** — *무엇부터 갈까요?* + 권장 시퀀스 1줄. (결정은 사용자.)

## 3. 위임 규약 (네이티브 `task` tool — 1차)

PM 은 ticket 구현/검토/설계를 직접 하지 않고 **내장 `task` tool 로 subagent 에 위임**한다.
위임 흐름은 `claim → 위임(dev) → 검토(reviewer) → finish` 다.

### 3.1 위임 = `task` tool 호출

PM(build primary)이 내장 `task` tool 을 호출한다 — opencode 가 `.opencode/agents/*.md`
(mode: subagent) 를 **별도 자식 세션**에서 구동하고 결과를 task 결과로 PM 에 돌려준다
(PM 9차 deciding test 실증 — opencode agent list 등록 + task tool json `"subagent_type"`/
`"output"` + 자식이 부모와 다른 sessionId·subagent `model:` 대로 구동).

task tool 인자:

- `subagent_type` — 위임 대상 (아래 §3.2 매핑: `developer` / `code-reviewer` / `architect` / `researcher`).
- `description` — 짧은 한 줄 (예: `"T-NNNN 구현"`).
- `prompt` — role 프롬프트 (§3.4/§3.5).

특성:

- subagent 의 `tools:`/`permission:`/`model:` (`.opencode/agents/*.md` frontmatter) 가 그대로
  권한·모델을 정한다 — `--agent build/plan` 분기·`-m` 모델 명시 **불필요**. 자식 세션이
  fresh 컨텍스트(200K 격리)에서 subagent 정의대로 구동한다 (실증).
- PM 은 task 결과로 위임 완료를 인지한다. 순차 위임(dev → reviewer)은 opencode 가 자식
  세션으로 관리한다.

### 3.2 role → subagent_type 매핑

| PM role | task `subagent_type` | 권한 (agent 정의가 강제) |
|---|---|---|
| orchestrator(PM) | (위임 안 함 — build primary 자신) | — |
| developer | `developer` | 쓰기 (read/edit/write/bash/glob/grep) |
| code-reviewer | `code-reviewer` | 읽기 (edit/write false — generate ≠ evaluate) |
| architect | `architect` | 설계 (읽기 + 문서 쓰기) |
| researcher | `researcher` | 읽기 (read/glob/grep/bash·edit/write false — gather, 조사·사실수집) |

> **위임 가이드** — researcher = bounded fact-gathering(여러 파일·로그·레퍼런스를 훑어 사실·인용·목록 추출).
> *결론만* 돌려받고, 여러 출처를 가로지르는 synthesis(교차 통찰)는 PM 이 직접 흡수한다(degrade 방지).

### 3.3 위임 전 사전 조건

- ticket 이미 claim (`pm` 세션) · depends_on 모두 done · touches 명시 · DoD verify-able.
- **컨텍스트 예산** — touches 가 대형 파일·광범위 읽기를 요구하면 dev 가 truncation 위험.
  본문이 정확한 함수/라인·패턴 reference 로 읽기를 좁히는지 확인 (안 되면 위임 전 본문 보강·분할).
- **병렬 위임 시 touches disjoint** — 동시 위임할 ticket 들의 touches 가 완전히 겹치지
  않을 때만. (task 병렬은 opencode 가 자식 세션을 관리한다. `opencode run` 폴백 경로의
  병렬은 세션 DB 락 가능성 — 미검증·순차 안전, §3.7 노트.)

### 3.4 위임 프롬프트 (developer)

ticket 본문이 self-contained 이므로 프롬프트는 짧다:

```
T-NNNN 을 구현하라.

세션명: orch-dev-TNNNN (board.py 조작은 PM 담당 · 너는 코드 + 테스트만).
ticket 본문은 다음으로 확인:
  {{PY}} .project_manager/tools/board.py show T-NNNN
본문이 단일 진실 — 목표/인터페이스/결정/DoD/참고 절대로 구현.

완료 시 보고:
- 변경 파일 목록
- 신규 테스트 수
- 전체 회귀 결과 ({{TEST_CMD}}: A / B passed)
- DoD 항목별 충족 evidence
```

### 3.5 위임 프롬프트 (code-reviewer)

```
T-NNNN 의 변경을 검토하라.
변경 파일: <touches 인자 그대로 인용>.
status.md / log 갱신은 PM 담당 — 그 누락은 dev must-fix 아님.

완료 시 보고:
- must-fix (수정 필수 · {{PROJECT_CONSTRAINTS}} 위반 · 결함)
- should-fix (권장 · 운영 영향)
- suggestion (개선 옵션)
- 통과/반려 명시
```

### 3.6 reviewer 후 PM 처리

- **PM 직접 fix** — 1줄·1패턴. cycle 시간 절약.
- **dev 재작업** — 여러 줄 또는 같은 file 작업 중.
- **별도 ticket 후보** — 본 ticket 범위 외.
- **reviewer cross-check** — reviewer 도 틀릴 수 있다. should-fix 처리 전 코드 흐름 독립
  점검 · 부정확이면 변경 불필요 + log 영구 기록.

### 3.7 외부 프로세스 진입 (폴백) — `opencode run`

`task` tool 을 못 쓰는 환경 — headless 자동화·CI·task tool 미노출 빌드 — 에서만 동일
인터페이스를 외부 프로세스로 띄운다:

```bash
opencode run --agent build --format json "<dev/architect 프롬프트>"   # 쓰기
opencode run --agent plan  --format json "<reviewer 프롬프트>"        # 읽기
```

- `--agent build` — 쓰기 권한 (dev·**architect** — 설계 초안 문서 쓰기 필요). `--agent plan` — 읽기 전용 (reviewer).
- **모델 = opencode 기본.** `--agent build/plan` 은 opencode **내장 primary** 라 우리 subagent
  (`.opencode/agents/*.md`)의 `model:` 필드를 읽지 않는다 (native task 1차와 다른 점 — 거긴 정의대로 구동).
  폴백서 Pro/특정 모델을 강제하려면 `-m <model>` 을 명시한다.
- `--format json` — ANSI escape 회피, 결과를 PM 이 파싱 가능하게.
- 컨텍스트는 프로세스마다 fresh → 200K 한도를 위임으로 격리. PM 은 exit code + json 결과로
  완료를 인지한다. **병렬 `opencode run` 은 세션 DB 락 가능성 — 미검증·순차 안전**
  (병렬 필요 시 XDG sandbox 격리 검토).

## 4. 작업 완료 부기 (PM 손)

위임 결과를 받고 ticket 을 닫을 때:

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
```

추가로 PM 이:
- `.project_manager/wiki/status.md` 해당 모듈 행 갱신.
- log 에 handoff entry append (핸드오프 시 `pm_handoff.py` 가 skeleton 생성).
- 회귀 `{{TEST_CMD}}` 통과 확인 (red 면 닫지 않는다).
- git commit — 논리적 체크포인트. 메시지 말미 `Co-Authored-By` 트레일러.

> **참조 규약**: ADR/ticket/idea 는 ID-wikilink(`[[ADR-NNNN]]`·`[[T-NNNN]]`·`[[idea-NNNN]]`)로만 —
> 생파일명·슬러그 금지(`board.py lint --gate` 강제). 규칙·이유·예시 단일 진실 = [[pm_playbook]] §참조 규약.

## 5. PM 결정 권한

> PM 은 *어떻게* 를 자율 결정한다. 사용자는 *무엇을 · 얼마의 비용으로 · 밖으로 내보낼지* 를 결정한다.

- **자율 + 사후 로그** — 새 ticket 발행 / super-ticket 분할 / depends_on 변경 / block·unblock /
  spec 추출 / 일상 ADR(`scope: internal-process`) / 위임. → log 가 사후 감사 경로.
- **사용자 게이트 (사전 동의)** — 미션·핵심 안전 경계 · 유료/한도 API 대량 호출 · 키 발급·
  외부 게시·배포 · `scope: mission` ADR. 상세는 [[pm_role.local.md]] §사용자 게이트.
- **금지 (PM·사용자 단독 불가)** — 미션 변경 · 핵심 안전 경계 약화 · 영구 수동 영역 자동화.
  양측 합의 + 별도 ADR 필요. 상세는 [[pm_role.local.md]] §금지.

## 6. 라이브 외부 행위 안전 가드

- 단위 테스트는 **모두 mock**. 라이브 외부 API 호출은 통합 테스트 마커로만.
- 외부 비가역 행위(네트워크 송신·배포·키 발급)가 가능한 ticket 은 사용자 명시 승인 후 진행.
- **프로덕션 진입점을 라이브로 실행하지 않는다** — 검증은 mock 격리된 자동 테스트뿐.
- 새 외부 비가역 행위엔 코드 차원 안전 가드(테스트 중 거부 · opt-in 환경변수)를 둔다.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 이 프로젝트의 아키텍처 불변식·안전 경계. 위임 프롬프트(§3.5)에도 인용된다.
     제약이 없으면 이 절을 통째로 삭제해도 된다. -->

## 7. 자주 쓰는 명령

엔진이 인코딩을 코드로 처리하므로 env prefix 없이 그대로 호출한다 (§1).

```bash
# 보드
{{PY}} .project_manager/tools/board.py list
{{PY}} .project_manager/tools/board.py show T-NNNN
{{PY}} .project_manager/tools/board.py claim T-NNNN --session pm
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --tag phase-1
{{PY}} .project_manager/tools/board.py lint           # depends_on·thin-ticket 검사

# log
{{PY}} .project_manager/tools/pm_log.py tail          # 마지막 entry (의미단위 읽기)
{{PY}} .project_manager/tools/pm_log.py archive --before YYYY-MM-DD

# 위임 (1차 = 네이티브 task tool · 모델은 subagent 정의가 정함)
#   PM 이 task tool 호출: subagent_type=developer|code-reviewer|architect, description, prompt (§3.1)
# 위임 폴백 (headless·CI·task tool 미노출 — 내장 build/plan primary 라 subagent model: 안 읽음;
#   모델은 opencode 기본, Pro/특정 모델 강제 시 -m <model>)
opencode run --agent build --format json "<dev/architect 프롬프트>"
opencode run --agent plan  --format json "<reviewer 프롬프트>"

# 핸드오프 (세션 종료)
{{PY}} .project_manager/tools/pm_handoff.py --dry-run

# 엔진 동기화 (메인테이너 · 루트 → 이 타깃)
{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run
```

## 8. 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py · pm_log.py (공유 엔진 · 0 수정) |
| `.project_manager/wiki/` | 비-코드 산출물 (작업/결정/사양/상태/**domain 지식 레이어**(§10)/pm_role·pm_state·pm_playbook/log/raw) |
| `.opencode/command/` | PM workflow slash command (skill 등가 · T-0003) — pm-wave-claim · pm-wave-finish · pm-dev-delegate · pm-bootstrap · pm-handoff · spike-new |
| `.opencode/agents/` | pm primary 정의 (mode: primary · relay deterministic spawn 타깃 · ADR-0009·ADR-0020) + researcher · architect · developer · code-reviewer subagent 정의 (mode: subagent · 4축 gather/design/build/evaluate · task tool 위임 1차 · T-0004) |
| `AGENTS.md` | 이 파일 — PM 부트스트랩·위임·인코딩 어댑터 (= claude_code 의 CLAUDE.md) |

## 9. 막혔을 때

- 의존 ticket 미완 / 외부 키 없음 → `board.py block --reason "..."`.
- 잘못 claim → `board.py unclaim`.
- ticket 본문 부족 → 먼저 본문 보강하고 계속 (본문이 단일 진실).
- 모르는 결정 필요 → ADR 작성 후 진행 (`.project_manager/wiki/decisions/`).
- 위임(task 결과 또는 폴백 프로세스 exit≠0)이 깨지거나 결과가 불완전 → 재위임 전에 ticket 본문·컨텍스트 예산 점검 (§3.3).

## 10. domain 지식 레이어 (살아있는 프로젝트 지식)

`.project_manager/wiki/domain/` = 이 프로젝트가 **무엇이고 어떻게 다루나**의 *살아있는* 지식 그래프.
`decisions/`(왜·동결)와 대비해 *현재 무엇·어떻게*를 계속 갱신한다(ADR-0018). **`architecture.md`
(현재-아키텍처 단일 진실·부트스트랩 1순위·ADR-0022)와 공존하는 그 *세부* 지식층**이다 — architecture.md
가 구조·모듈·구현상태를 한 장으로 잡고, domain 페이지가 `covers:` 코드 글롭 단위로 세부(개념·절차·조사)를
깊게 편다 (refines ADR-0018). architecture↔domain 충돌 = 의도↔현실 드리프트 표면화 기능.

**페이지 작성** — `domain/_template.md` 를 복사해 `domain/<주제>.md`. frontmatter:
- `type:` concept(무엇·왜) | guide(어떻게·절차) | research(조사·누적)
- `covers:` 이 페이지가 담당하는 코드 글롭 (예 `src/foo/**`). 코드-무관 개념이면 비움.
- `derived:` false(사람 author) | true(코드서 자동생성·손대지 마)

한 페이지 = 한 가지. `[[다른-페이지]]` 로 링크 → 그게 곧 그래프(wikilink lint 가 검증).

**CLI (`domain.py`):**
```bash
{{PY}} .project_manager/tools/domain.py list                      # 페이지 카탈로그 (type·covers·stale)
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN  # ticket touches 와 겹치는 covers 페이지 (소환)
{{PY}} .project_manager/tools/domain.py capture --tickets T-NNNN  # touch∩covers 갱신 reminder (채록)
{{PY}} .project_manager/tools/domain.py lint                      # freshness — stale 페이지 검사
```

**살아있는 루프** — 코드 touch → 겹치는 페이지 **소환**(`domain affected`) → 갱신 reminder(`domain
capture`) → 채록 → `covers` 코드가 페이지 `updated` *후* 바뀌면 **stale** ⚠ 로 가시화(`domain lint`).
*막지 않고 보이게* — 틀린 정보 조용한 참조 방지. PM 은 위임 전 `domain affected` 로 영향 페이지를
소환해 dev 프롬프트에 동반하고, 완료 시 `domain capture` 로 채록을 챙긴다.

## 참고

- `.project_manager/wiki/pm_role.md` — PM 책임·결정 권한·핸드오프 단일 진실
- `.project_manager/wiki/pm_playbook.md` — Wave 패턴·메타 정책 (필요 시 Read)
- ADR-0006 (`.project_manager/wiki/decisions/`) — opencode 어댑터 결정 (위임·인코딩·모델·self-driven)
