# AGENTS.md — opencode PM 어댑터 (lite 진입·경량·자족)

> opencode 세션 진입점 **lite 판**. 이 한 파일 + 공유 엔진(`board.py`) + `.opencode/command/`
> 만으로 PM happy-path(부트스트랩 → ticket 발행 → 위임 → finish)를 자족 운영하도록 압축했다.
> Claude Code 비의존 — opencode LLM(로컬 gemma / 회사 Pro)이 이 문서만 읽고 PM 을 운영한다.
> **무거운 방법론(`pm_role.md`·`pm_playbook.md`)은 auto-load 하지 않는다** — happy-path 밖일 때만
> lazy Read(§9·§참고). 회사 200K 배포 1급. (2D 무게축 lite-A · ADR-0006)

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 0. opencode 실행 모델

- **build primary = PM(orchestrator).** 이 문서를 읽은 build 세션이 곧 PM 이다.
- **위임 = 네이티브 `task` tool** — opencode 가 `.opencode/agents/*.md` subagent 를 별도 자식 세션(fresh ctx = 200K 격리·자식 model/권한이 subagent 정의대로)에서 구동(PM 9차 실증). 폴백 = `opencode run` 외부 프로세스. §3.
- **엔진 = 공유 python**(`.project_manager/tools/*.py`). PM 은 bash 로 호출·해석. **엔진 0 수정** — 어댑터만 타깃별.
- **인코딩**: 엔진이 코드로 처리(PM 7차·C1 파일·C2 콘솔 reconfigure) — env prefix 불필요. PowerShell/CP949 서도 env 없이 한글 깨짐 0. 드물게 필요하면 셸별 문법(PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`).

## 1. 부트스트랩 (세션 시작)

```bash
{{PY}} .project_manager/tools/board.py list   # 잡을 수 있는 ticket
{{PY}} .project_manager/tools/pm_log.py tail   # 직전 핸드오프(마지막 entry)
```

1. **이 파일** — 이미 로드됨. 2. **보드**(위) · **아키텍처** [`architecture.md`](.project_manager/wiki/architecture.md)(현재-아키텍처 단일 진실 · ① live / ② target · ADR-0022 · 부트스트랩 1순위·충돌 시 이게 기준) ·
**상태** [`status.md`](.project_manager/wiki/status.md)(모듈 진행상태·비고 · judgment-only) +
[`pm_state.md`](.project_manager/wiki/pm_state.md)(per-clone·없으면 `board.py init` 이 생성) · 3. **직전 핸드오프**(위 `pm_log.py tail`).

> 세션명 **`pm`** 고정 — `board.py ... --session pm`. 위임(task subagent·폴백 프로세스) 식별 라벨 `orch-dev-TNNNN`/`orch-review-TNNNN`.
> 첫 turn 보고: board 1줄 + 직전 요약 3~5줄 + 다음 옵션 + 결정 요청(*무엇부터?*). 기계 dump = `pm_bootstrap.py`.

## 2. 작업 원칙 (반드시)

- **작은 단위 → 단계별 테스트.** 한 모듈=한 ticket=한 단계. **테스트 없이는 안 끝났다**(회귀 `{{TEST_CMD}}` green).
- **최소 변경**(ticket 요구만) · **명시적 풀네임**.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 아키텍처 불변식·안전 경계. 위임 프롬프트(§3)에도 인용. 없으면 이 절 삭제. -->

## 3. 위임 규약 (네이티브 `task` tool — 1차)

`claim → 위임(dev) → 검토(reviewer) → finish`. PM 은 직접 구현하지 않는다.

- **1차 = `task` tool 호출** — 인자 `subagent_type`(=`developer`|`code-reviewer`|`architect`)·`description`(한 줄)·`prompt`(role 프롬프트). opencode 가 subagent(`.opencode/agents/*.md`)를 별도 자식 세션(fresh ctx·200K 격리)에서 구동·결과 반환(PM 9차 실증). subagent `tools:`/`permission:`/`model:` 이 권한·모델을 정함 — `--agent`/`-m` 분기 불필요.
- **role → subagent_type**: developer=쓰기(코드+테스트) · code-reviewer=읽기(generate≠evaluate) · architect=설계(읽기+문서 쓰기).
- **사전조건**: ticket claim(`pm`)·depends_on done·touches 명시·DoD verify-able. **병렬은 touches disjoint 일 때만**(task 병렬은 opencode 가 자식 세션 관리).
- dev 프롬프트 골자: "T-NNNN 구현. 본문 단일진실(`board.py show T-NNNN`). board/status/log 는 PM — 너는 코드+테스트. 보고: 변경파일·테스트수·회귀결과·DoD evidence."
- reviewer 후: **PM 직접 fix**(1줄·1패턴) / dev 재작업(여러 줄) / 별도 ticket(범위 외). reviewer 도 틀릴 수 있다 — should-fix 는 흐름 cross-check 후.
- **폴백** (headless·CI·task tool 미노출): `opencode run --agent build|plan --format json "<프롬프트>"` (dev·architect=build 쓰기, reviewer=plan 읽기). `--agent build/plan` 은 내장 primary 라 subagent `model:` 을 안 읽음 → 모델은 opencode 기본(Pro 강제는 `-m`). 병렬 폴백은 세션 DB 락 가능성·순차 안전.

## 4. ticket 발행 계약 (PM 자족 — board.py new)

```bash
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --depends T-0001 --tag phase-1
```

본문 필수 절(self-containment — `board.py lint` 가 thin·dangling 검사): **목표**(무엇·왜) · **인터페이스**(시그니처·경로) ·
**결정**(분기 근거) · **완료 조건(DoD)**(체크 가능: 테스트 green·산출·판정/비고) · **참고**(`[[wikilink]]` 실재 · `depends_on`/`blocks` frontmatter).
**본문이 단일 진실 — 그것만으로 구현 가능해야.** 채번·area prefix·thin 세부는 필요 시 `pm_playbook.md` Read.

## 5. 완료 부기 (PM 손)

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
```

추가: `status.md` 모듈 행 · `log/current.md` entry · 회귀 `{{TEST_CMD}}` green · git commit(`Co-Authored-By` 트레일러).
`.opencode/command/` 의 pm-* 커맨드가 스칼라·skeleton·stage 자동화.

## 6. 결정 권한 (요약)

- **자율 + 사후 log** — ticket 발행/분할·depends_on 변경·block/unblock·spec 추출·일상 ADR(`scope: internal-process`)·위임.
- **사용자 게이트(사전 동의)** — 미션·핵심 안전 경계·유료/한도 API 대량·키 발급·외부 게시·배포·`scope: mission` ADR.
- **금지(단독 불가)** — 미션 변경·안전 경계 약화·영구 수동 영역 자동화(양측 합의+ADR). 상세는 [[pm_role.local.md]].

## 7. 라이브 외부 행위 안전 가드

- 단위 테스트 전부 mock. 라이브 외부 호출은 통합 마커로만. **프로덕션 진입점 라이브 실행 금지**(검증=mock 자동 테스트).
- 외부 비가역 행위(송신·배포·키 발급) ticket 은 사용자 명시 승인 후. 새 비가역 행위엔 코드 안전 가드(opt-in env).

## 8. 자주 쓰는 명령 / 핵심 디렉토리

```bash   # 엔진이 인코딩을 코드로 처리 — env prefix 불필요
{{PY}} .project_manager/tools/board.py list|show|claim|complete|new|lint
{{PY}} .project_manager/tools/pm_log.py tail
{{PY}} .project_manager/tools/pm_handoff.py --dry-run            # 핸드오프
{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run   # 엔진 동기화(메인테이너)
```

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | 공유 엔진 board.py·pm_*.py (0 수정) |
| `.project_manager/wiki/` | status·pm_state / **domain**(살아있는 지식·`domain.py`) / pm_role·pm_playbook(lazy) / log / decisions / raw |
| `.opencode/command/` · `.opencode/agents/` | PM workflow 커맨드 · subagent(task tool 위임 1차) |
| `AGENTS.md` | 이 파일(= claude_code 의 CLAUDE.md lite) |

## 9. 막혔을 때 / lazy 참조 (happy-path 밖 → 그때 Read)

- 의존 미완·키 없음 → `board.py block --reason` · 잘못 claim → `board.py unclaim` · 본문 부족 → 보강 후 계속.
- 위임 깨짐(task 결과 또는 폴백 프로세스 exit≠0)·결과 불완전 → 재위임 전 본문·컨텍스트 예산 점검.
- **domain 지식 레이어**(살아있는 프로젝트 지식·`domain.py list/affected/capture/lint`·stale 가시화) →
  사용법 full [`AGENTS.md`](AGENTS.md) §10 / ADR-0018.
- **복잡 운영**(wave 충돌·incident·핸드오프 심층·멀티-PM·결정권한 경계·프레임워크 갱신) →
  [`pm_role.md`](.project_manager/wiki/pm_role.md)·[`pm_playbook.md`](.project_manager/wiki/pm_playbook.md) Read.
- 모르는 구조 결정 → ADR(`.project_manager/wiki/decisions/`).

## 참고

- `.project_manager/wiki/pm_role.md` — PM 책임·결정 권한·핸드오프 단일 진실 (lazy)
- `.project_manager/wiki/pm_playbook.md` — Wave 패턴·메타 정책 (lazy)
- ADR-0006 — opencode 어댑터 결정 (위임·인코딩·모델·self-driven)
