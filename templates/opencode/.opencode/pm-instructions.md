# pm-instructions.md — opencode PM 운영 지침 (실행 모델 · 위임)

> opencode-고유 PM 운영 전문. `opencode.jsonc` 의 `instructions` 배열로 **AGENTS.md 공통 코어와
> 함께 자동 로드**된다 — 공통 코어(프로젝트 정체성·엔진 호출[인코딩]·완료 기록·결정 권한·안전
> 가드) 위에 opencode 실행 모델과 위임 규약을 얹는다.
>
> 대응 관계: 이 파일 = Claude Code 타깃 CLAUDE.md 의 opencode-고유 부분. 그대로 번역이 아니라
> opencode 실행 모델(pm primary + 네이티브 `subagent` 도구 위임)에 맞게 재서술했다.
> **@source 전파** — 이 지침의 방법론 갱신은 pm_update 로 채택자에 도달한다(instance-owned
> AGENTS.md 와 대비). (ADR-0006 · amended by ADR-0069 — 진입 doc 공통 코어 + 하네스별 전달 채널)

## 1. opencode 실행 모델 (PM 멘탈 모델)

- **PM(orchestrator) = `pm` primary agent.**
  - **1차 = `pm` primary** (`.opencode/agents/pm.md` · `mode: primary`). relay(ADR-0009·세션 회전
    supervisor·ADR-0020 개명)가 PM 세션을 **deterministic 하게 spawn** 하는 타깃이다 — `opencode run --agent pm` 으로 올바른
    모델(Pro)·풀권한·안전 가드가 박힌 PM 세션이 뜬다. pm.md 본문은 thin — 공통 코어 AGENTS.md +
    이 지침으로 부트스트랩하라고 가리킨다.
  - opencode 2.3.0 실측에서 custom primary(`--agent pm`)가 직접 기동된다. PM 카드의 `permission.task`
    (opencode 2.x 가 `subagent` 도구 권한으로 읽는 옛 형식 키)는 architect/developer/code-reviewer/researcher
    네 역할만 허용하며 임의 subagent 위임은 막는다.
- **위임 = 네이티브 `subagent` 도구.** PM 은 dev/architect 역할을 내장 `subagent` 도구로
  위임한다 — opencode 가 `.opencode/agents/*.md` (`mode: all`) 를 **별도 자식 세션**에서
  백그라운드로 구동하고(새 컨텍스트 · 자식 model/권한이 subagent 정의대로), 자식이 끝나면 마지막
  응답을 `<subagent>` 메시지로 돌려준다. §2 위임 규약.
- **엔진 = 공유 python.** PM 운영 로직은 `.project_manager/tools/*.py` (board.py·pm_*.py)에
  있다. PM 은 셸 도구(`shell`)로 이 CLI 를 호출·해석한다. **엔진은 0 수정** — 어댑터(공통 코어
  AGENTS.md·이 지침·`.opencode/`)만 타깃별로 다르다.
- **config 는 실행 중에도 다시 읽힌다.** opencode 2.x 는 `opencode.jsonc`·에이전트 카드
  (`.opencode/agents/*.md`)·플러그인(`.opencode/plugins/`·`lib/`)·`instructions` 배열의 지침 파일을
  감시해 바뀌면 다시 읽는다. 2.3.0 실측: 카드 본문·권한 변경은 다음 요청의 시스템 프롬프트·도구
  목록에 반영되고, 이 지침(`.opencode/pm-instructions.md`)의 변경은 `<system-update>` 변경분 메시지로 온다.
- **셸 도구 시간 제한**: opencode 셸 도구(`shell`)는 `timeout` 을 주지 않은 명령을 120초에 끊는다
  (2.3.0 은 멈추고 2.3.1 이상은 백그라운드로 넘긴다). 이 기본값을 바꾸는 설정·환경 변수는 없다. 대형 repo
  `worktree add`·cross 위임(`pm_delegate.py`)·추가 리뷰어처럼 오래 걸리는 엔진 명령은 호출마다
  **`timeout: 29300000`**(ms, 8시간 8분 20초)을 준다. 카드가 "Claude PM은 … `timeout: 29300000`(ms)을
  반드시 명시한다" 고 적은 명령은 opencode PM 에도 같다. 엔진 타임아웃은 `PM_GIT_TIMEOUT`(초·`none`=무제한).
  상세는 `/pm-env` 스킬 §timeout 노브.

## 2. 위임 규약 (네이티브 `subagent` 도구)

PM 은 ticket 구현/검토/설계를 직접 하지 않는다. transport 규칙은 한 문장이다. 설계·구현·리뷰·조사는 target 하네스 == PM 하네스면 native, 다르면 `pm_delegate.py` cross 다. 위임 흐름은 claim → 설계·구현
(native `subagent`) → 리뷰(같은 하네스면 `ticket prepare --cluster C-<ticket-group> --role code-reviewer` 뒤 `subagent`) → finish 다.
리뷰 절차는 `/pm-dev-delegate` 카드의 code-reviewer 위임 절이 단일 진실이다.

### 2.1 위임 = `subagent` 도구 호출

PM(primary)이 내장 `subagent` 도구를 호출한다 — opencode 가 `.opencode/agents/*.md`
(`mode: all`) 를 **별도 자식 세션**에서 백그라운드로 구동한다. 호출은 곧바로 "running" 을 돌려주고,
자식이 끝나면 그 자식의 **마지막 응답**이 `<subagent ... state="completed">` 메시지로 PM 에 온다
(opencode 2.3.0·2.3.2 실측).

`subagent` 도구 인자:

- `agent` — 위임 대상 (아래 §2.2 매핑: `developer` / `architect` / `code-reviewer` / `researcher`).
- `description` — 짧은 한 줄 (예: `"T-NNNN 구현"`).
- `prompt` — role 프롬프트 (§2.4).

특성:

- subagent 의 `permission:`/`model:` (`.opencode/agents/*.md` frontmatter) 가 그대로
  권한·모델을 정한다. `model` 인자는 주지 않는다. 자식 세션은 새 컨텍스트에서 subagent 정의대로
  구동한다.
- PM 은 `<subagent>` 메시지로 위임 완료를 인지한다. 진행을 묻거나 기다리는 명령을 돌리지 않는다.
  순차 위임(architect → developer)은 앞 위임의 `<subagent>` 메시지를 받은 뒤 다음을 부른다.
- 자식은 마지막 응답만 돌려준다. 역할 카드의 대형 산출 파일 전달 규약이 이 때문에 필요하다.

### 2.2 role → `agent` 매핑

| PM role | `subagent` 의 `agent` | 권한 (agent 정의가 강제) |
|---|---|---|
| orchestrator(PM) | (위임 안 함 — pm primary 자신) | board/status/log 쓰기 + 역할 위임 |
| developer | `developer` | 쓰기 (read/edit/write/bash/glob/grep) |
| architect | `architect` | 설계 (읽기 + 문서 쓰기) |
| code-reviewer | `code-reviewer` | 검토 (읽기 + 테스트 + 지정 라운드 파일 쓰기) |
| researcher | `researcher` | 기계적 읽기(read/glob/grep, bash/edit deny — gather) |

> 리뷰 target 하네스가 opencode 가 아니면 `pm_delegate.py --role code-reviewer --cluster` 가 그 하네스를
> 별도 프로세스로 실행한다.
>
> **위임 가이드** — researcher = bounded fact-gathering(여러 파일·로그·레퍼런스를 훑어 사실·인용·목록 추출).
> *결론만* 돌려받고, 여러 출처를 가로지르는 synthesis(교차 통찰)는 PM 이 직접 받아 반영한다(degrade 방지).

### 2.3 위임 전 사전 조건

- ticket 이미 claim (claim 정체성 `--task <task>`) · depends_on 모두 done · touches 명시 · DoD verify-able.
- **컨텍스트 예산** — touches 가 대형 파일·광범위 읽기를 요구하면 dev 가 truncation 위험.
  본문이 정확한 함수/라인·패턴 reference 로 읽기를 좁히는지 확인 (안 되면 위임 전 본문 보강·분할).
- **병렬 위임 시 touches disjoint** — 동시 위임할 ticket 들의 touches 가 완전히 겹치지
  않을 때만. (`subagent` 병렬은 opencode 가 자식 세션을 관리한다.)

### 2.4 위임 프롬프트 (developer)

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

### 2.5 리뷰 (code-reviewer)

손 프롬프트가 없다 — 엔진이 프롬프트 파일·라운드 자리를 만든다. 같은 하네스면 `ticket prepare --cluster
C-<ticket-group> --role code-reviewer` 가 낸 `prompt` 파일 경로를 `subagent`(`agent: code-reviewer`)에 넘기고
`<subagent>` 메시지를 받은 뒤 `ticket collect` 로 돌려받는다. `/pm-dev-delegate` 카드의 code-reviewer 위임 절을 따른다.

### 2.6 reviewer 후 PM 처리

- **PM 직접 fix** — 1줄·1패턴. cycle 시간 절약.
- **dev 재작업** — 여러 줄 또는 같은 file 작업 중.
- **별도 ticket 후보** — 본 ticket 범위 외.
- **reviewer cross-check** — reviewer 도 틀릴 수 있다. should-fix 처리 전 코드 흐름 독립
  점검 · 부정확이면 변경 불필요 + log 영구 기록.

### 2.7 다른 하네스 PM 의 opencode 위임 — `opencode run`

target 하네스가 opencode 이고 PM 하네스가 다르면 엔진(`pm_delegate.py`)이 같은 역할 카드를
`opencode run --agent <role> --format json --standalone` 자식 프로세스로 실행한다(프롬프트는 `--file`).
네 역할 카드는 `mode: all`이므로 `run --agent <role>`도 native `subagent` 와 같은 custom
prompt·model·permission을 쓴다. build/plan으로 축약하거나 default agent로 낮추지 않는다. opencode PM 은
이 경로를 손으로 띄우지 않고 §2.1 의 `subagent` 도구를 쓴다.

## 참고

- `AGENTS.md` — harness-neutral 공통 코어(프로젝트 정체성·엔진 호출·완료 기록·결정 권한·안전 가드).
- `.project_manager/wiki/pm_role.md` — PM 책임·결정 권한·핸드오프 단일 진실
- `.project_manager/wiki/pm_playbook.md` — 묶음 진행 패턴·메타 정책 (필요 시 Read)
- ADR-0006 (`.project_manager/wiki/decisions/`) — opencode 어댑터 결정 (위임·인코딩·모델·self-driven)
- ADR-0069 — 진입 doc 공통 코어 + 하네스별 전달 채널 (이 지침의 전달 채널 근거)

## PM-workflow 두 진입점

opencode 는 사람 슬래시 팔레트와 모델 스킬 진입점을 따로 만든다(opencode 2.3.0).

- `.opencode/command/*.md` — 팔레트 진입(`/pm-bootstrap` 등 15개). 입력한 인자를 그대로 전달한다.
- `.claude/skills/<skill>/SKILL.md` — canonical 저작 소스이자 모델 `skill` tool 이 읽는 파일.

command 파일은 canonical 에서 기계 생성한 사본이라 손으로 편집하지 않는다. 두 진입점은 서로를
대체하지 않으므로 한쪽만 출하하면 나머지 진입이 사라진다.
