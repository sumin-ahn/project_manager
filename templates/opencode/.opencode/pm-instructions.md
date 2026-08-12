# pm-instructions.md — opencode PM 운영 지침 (실행 모델 · 위임)

> opencode-고유 PM 운영 전문. `opencode.jsonc` 의 `instructions` 배열로 **AGENTS.md 공통 코어와
> 함께 자동 로드**된다 — 공통 코어(프로젝트 정체성·엔진 호출[인코딩]·완료 부기·결정 권한·안전
> 가드) 위에 opencode 실행 모델과 위임 규약을 얹는다.
>
> 대응 관계: 이 파일 = Claude Code 타깃 CLAUDE.md 의 opencode-고유 부분. 그대로 번역이 아니라
> opencode 실행 모델(build primary + 네이티브 `task` tool 위임)에 맞게 재서술했다.
> **@source 전파** — 이 지침의 방법론 갱신은 pm_update 로 채택자에 도달한다(instance-owned
> AGENTS.md 와 대비). (ADR-0006 · amended by ADR-0069 — 진입 doc 공통 코어 + 하네스별 전달 채널)

## 1. opencode 실행 모델 (PM 멘탈 모델)

- **PM(orchestrator) = `pm` primary agent (1차) · build primary (폴백).**
  - **1차 = `pm` primary** (`.opencode/agents/pm.md` · `mode: primary`). relay(ADR-0009·세션 회전
    supervisor·ADR-0020 개명)가 PM 세션을 **deterministic 하게 spawn** 하는 타깃이다 — `opencode run --agent pm` 으로 올바른
    모델(Pro)·풀권한·안전 가드가 박힌 PM 세션이 뜬다. pm.md 본문은 thin — 공통 코어 AGENTS.md +
    이 지침으로 부트스트랩하라고 가리킨다.
  - **폴백 = build primary.** 회사판 opencode 가 custom primary(`mode: primary`)를 노출/허용하는지
    **미검증**(opencode-pm-adapter spike §6)이므로, `pm` primary agent 가 안 떠도 PM 부트스트랩이 안 깨지게 한다 —
    공통 코어 + 이 지침을 읽은 build 세션도 곧 PM 이다(plan/build 두 타입만 노출해도 무관). **PM 동작의
    단일 진실은 AGENTS.md 공통 코어 + 이 지침**이므로 어느 진입점이든 동일하게 PM 으로 구동된다.
    (additive — ADR-0006 amendment, 비준은 PM)
- **위임 = 네이티브 `task` tool.** PM 은 dev/reviewer/architect 역할을 내장 `task` tool 로
  위임한다 — opencode 가 `.opencode/agents/*.md` (mode: subagent) 를 **별도 자식 세션**에서
  구동한다 (fresh 컨텍스트 = 200K 격리 · 자식 model/권한이 subagent 정의대로 — PM 9차 실증).
  **폴백 = `opencode run` 외부 프로세스** (headless·CI·task tool 미노출 빌드). §2 위임 규약.
- **엔진 = 공유 python.** PM 운영 로직은 `.project_manager/tools/*.py` (board.py·pm_*.py)에
  있다. PM 은 bash tool 로 이 CLI 를 호출·해석한다. **엔진은 0 수정** — 어댑터(공통 코어
  AGENTS.md·이 지침·`.opencode/`)만 타깃별로 다르다.
- **config = 프로세스 시작 시 로드·캐싱 (변경은 재시작 후 반영).** opencode 는
  `opencode.jsonc`·플러그인(`.opencode/plugins/`·`lib/`)·에이전트 frontmatter
  (`.opencode/agents/*.md`)를 **세션 프로세스 시작 시 한 번 읽어 캐싱**한다 — 실행 중 이 파일들을
  고쳐도 **그 세션엔 반영되지 않는다**(PM 59 라이브 실측). 권한·모델·플러그인·ctx-guard 노브를
  바꿨으면 opencode 를 **재시작**해야 새 값이 산다 (위임된 subagent 도 부모 세션 시작 시점의
  config 로 뜬다). 이 지침(`.opencode/pm-instructions.md`)·공통 코어(`AGENTS.md`)도 `instructions`
  배열로 세션 시작 시 로드된다.
- **bash 툴 timeout (worktree add false-kill 방지·T-0293)**: 대형 repo `worktree add`(full checkout·느린
  디스크/VPN)가 opencode bash 툴 기본 120초에 죽으면, opencode 실행 쉘에 **`export OPENCODE_EXPERIMENTAL_
  BASH_DEFAULT_TIMEOUT_MS=29300000`**(8시간 8분 20초)을 상속시킨다 — opencode 는 config 파일로 못 실어(`.env` 미로드·
  실측) shell export/`.envrc`(direnv) 필요. `EXPERIMENTAL` = 버전 의존(회사 버전서 라이브 확인). 엔진
  타임아웃은 `PM_GIT_TIMEOUT`(초·`none`=무제한). 상세는 `/pm-env` 스킬 §timeout 노브.

## 2. 위임 규약 (네이티브 `task` tool — 1차)

PM 은 ticket 구현/검토/설계를 직접 하지 않고 **내장 `task` tool 로 subagent 에 위임**한다.
위임 흐름은 `claim → 위임(dev) → 검토(reviewer) → finish` 다.

### 2.1 위임 = `task` tool 호출

PM(build primary)이 내장 `task` tool 을 호출한다 — opencode 가 `.opencode/agents/*.md`
(mode: subagent) 를 **별도 자식 세션**에서 구동하고 결과를 task 결과로 PM 에 돌려준다
(PM 9차 deciding test 실증 — opencode agent list 등록 + task tool json `"subagent_type"`/
`"output"` + 자식이 부모와 다른 sessionId·subagent `model:` 대로 구동).

task tool 인자:

- `subagent_type` — 위임 대상 (아래 §2.2 매핑: `developer` / `code-reviewer` / `architect` / `researcher`).
- `description` — 짧은 한 줄 (예: `"T-NNNN 구현"`).
- `prompt` — role 프롬프트 (§2.4/§2.5).

특성:

- subagent 의 `tools:`/`permission:`/`model:` (`.opencode/agents/*.md` frontmatter) 가 그대로
  권한·모델을 정한다 — `--agent build/plan` 분기·`-m` 모델 명시 **불필요**. 자식 세션이
  fresh 컨텍스트(200K 격리)에서 subagent 정의대로 구동한다 (실증). **단 frontmatter 를 고쳤으면
  opencode 를 재시작해야 반영된다** — config 는 프로세스 시작 시 캐싱된다(§1 config 캐싱).
- PM 은 task 결과로 위임 완료를 인지한다. 순차 위임(dev → reviewer)은 opencode 가 자식
  세션으로 관리한다.

### 2.2 role → subagent_type 매핑

| PM role | task `subagent_type` | 권한 (agent 정의가 강제) |
|---|---|---|
| orchestrator(PM) | (위임 안 함 — build primary 자신) | — |
| developer | `developer` | 쓰기 (read/edit/write/bash/glob/grep) |
| code-reviewer | `code-reviewer` | 읽기 (edit/write false — generate ≠ evaluate) |
| architect | `architect` | 설계 (읽기 + 문서 쓰기) |
| researcher | `researcher` | 읽기 (read/glob/grep/bash·edit/write false — gather, 조사·사실수집) |

> **위임 가이드** — researcher = bounded fact-gathering(여러 파일·로그·레퍼런스를 훑어 사실·인용·목록 추출).
> *결론만* 돌려받고, 여러 출처를 가로지르는 synthesis(교차 통찰)는 PM 이 직접 흡수한다(degrade 방지).

### 2.3 위임 전 사전 조건

- ticket 이미 claim (세션 정체성 canonical `<repo>_<N>` · 솔로 M=1 은 생략) · depends_on 모두 done · touches 명시 · DoD verify-able.
- **컨텍스트 예산** — touches 가 대형 파일·광범위 읽기를 요구하면 dev 가 truncation 위험.
  본문이 정확한 함수/라인·패턴 reference 로 읽기를 좁히는지 확인 (안 되면 위임 전 본문 보강·분할).
- **병렬 위임 시 touches disjoint** — 동시 위임할 ticket 들의 touches 가 완전히 겹치지
  않을 때만. (task 병렬은 opencode 가 자식 세션을 관리한다. `opencode run` 폴백 경로의
  병렬은 세션 DB 락 가능성 — 미검증·순차 안전, §2.7 노트.)

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

### 2.5 위임 프롬프트 (code-reviewer)

```
T-NNNN 의 변경을 검토하라.
변경 파일: <touches 인자 그대로 인용>.
status.md / log 갱신은 PM 담당 — 그 누락은 dev must-fix 아님.

완료 시 보고:
- must-fix (수정 필수 · 프로젝트 고유 제약[AGENTS.md §프로젝트 고유 제약] 위반 · 결함)
- should-fix (권장 · 운영 영향)
- suggestion (개선 옵션)
- 통과/반려 명시
```

### 2.6 reviewer 후 PM 처리

- **PM 직접 fix** — 1줄·1패턴. cycle 시간 절약.
- **dev 재작업** — 여러 줄 또는 같은 file 작업 중.
- **별도 ticket 후보** — 본 ticket 범위 외.
- **reviewer cross-check** — reviewer 도 틀릴 수 있다. should-fix 처리 전 코드 흐름 독립
  점검 · 부정확이면 변경 불필요 + log 영구 기록.

### 2.7 외부 프로세스 진입 (폴백) — `opencode run`

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

## 참고

- `AGENTS.md` — harness-neutral 공통 코어(프로젝트 정체성·엔진 호출·완료 부기·결정 권한·안전 가드).
- `.project_manager/wiki/pm_role.md` — PM 책임·결정 권한·핸드오프 단일 진실
- `.project_manager/wiki/pm_playbook.md` — Wave 패턴·메타 정책 (필요 시 Read)
- ADR-0006 (`.project_manager/wiki/decisions/`) — opencode 어댑터 결정 (위임·인코딩·모델·self-driven)
- ADR-0069 — 진입 doc 공통 코어 + 하네스별 전달 채널 (이 지침의 전달 채널 근거)

## PM-workflow 진입 두 표면

opencode 는 사람 슬래시 팔레트와 모델 스킬 표면을 따로 만든다(1.18.16 실측).

- `.opencode/command/*.md` — 팔레트 진입(`/pm-bootstrap` 등 15개). 입력한 인자를 그대로 전달한다.
- `.claude/skills/<이름>/SKILL.md` — canonical 저작 소스이자 모델 `skill` tool 표면.

command 파일은 canonical 에서 기계 생성한 사본이라 손으로 편집하지 않는다. 두 표면은 서로를
대체하지 않으므로 한쪽만 출하하면 나머지 진입이 사라진다.
