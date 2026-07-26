# AGENTS.md — PM 어댑터 공통 코어

> 세션 진입점의 **harness-neutral 공통 코어**. 프로젝트 정체성·엔진 호출 규약(인코딩)·작업 완료
> 부기·PM 결정 권한·안전 가드 — 어느 하네스에서든 참인 PM 운영의 기반이다. 어느 하네스의 LLM
> 이든 이 문서를 읽고 PM 을 부트스트랩·운영한다.
>
> 하네스-고유 실행 모델·위임 규약은 각 하네스의 **네이티브 채널**로 별도 전달된다 — 이 공통
> 코어는 그 위에 공유된다(하네스별 운영 지침이 자동 로드돼 얹힌다). 어느 진입점이든 공통 코어 +
> 하네스 지침으로 동일하게 PM 으로 구동된다. (ADR-0006 · amended by ADR-0069)

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 1. 엔진 호출 규약 (인코딩)

엔진 python CLI 는 env prefix 없이 그대로 호출한다:

```bash
{{PY}} .project_manager/tools/board.py list
```

- 엔진이 인코딩을 코드로 처리(PM 7차·C1 파일 IO `encoding="utf-8"`·C2 콘솔 stdout reconfigure) —
  env prefix 불필요. Windows/CP949·PowerShell 환경서도 env 없이 한글 ticket·wiki 깨짐 0 으로 동작(실측).
- 구버전 Windows·서드파티 파이프서 드물게 필요하면 **각 셸 문법으로** 붙인다 —
  PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`. (bash 문법을 규약으로 강제하지 않는다.)
- **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 금지, 도구의 workdir
  파라미터나 명령 분리로 실행한다. 루트 facade 는 Windows 에선 `.\pm-config.cmd`·`.\pm-update.cmd`(bash 불요).

> 인터프리터: `{{PY}}` 는 setup 시 채택 환경의 인터프리터로 치환된다
> (`.project_manager/local.conf` 의 `py=` 가 단일 진실 — `board.py init` 이 설정 ·
> venv 면 `venv/bin/python`).

## 2. PM 부트스트랩 (세션 시작 시 순서)

PM 세션이 시작되면 다음을 순서대로 수행한다. **Read tool 로 파일을 읽을 땐 절대 경로를 쓴다.**

1. **이 문서(AGENTS.md·공통 코어)** — 이미 로드됨. 엔진 호출(인코딩) 규약(§1) 파악. 하네스-고유
   실행 모델·위임 규약은 각 하네스의 네이티브 채널이 별도로 전달한다(공통 코어와 함께 로드).
2. **PM 운영 매뉴얼** — `.project_manager/wiki/pm_role.md` (정적 운영 매뉴얼: 책임·결정 권한·핸드오프).
3. **PM 동적 상태** — task 모드는 `.project_manager/.local/tasks/<task>/pm_state.md`가 연속성의
   단일 앵커다. slot 모드는 `.project_manager/.local/slots/<repo>_<N>/pm_state.md`
   (`<repo>_<N>` = worktree `work/<repo>_<N>` basename), 솔로는 `wiki/pm_state.md` legacy
   폴백(T-0166/ADR-0033). 모두 세션 window·진행 중 의사결정·남은 작업을 담는 git-ignored 상태다.
   신규 task는 `/pm-bootstrap --task <이름>` 진입 즉시 이 파일을 만들므로 호출 전에는 없어도
   정상이다. 기존 task의 파일이 빠졌어도 같은 진입에서 복구한다. slot/solo state가 없으면 채택
   setup 미완 — `board.py init` 이 template 에서 생성한다.
4. **현재-진실 + 진행 상태** — `.project_manager/wiki/architecture.md`(**현재-아키텍처 단일 진실**·① live / ② target · ADR-0022 · 부트스트랩 1순위·충돌 시 기준) → `.project_manager/wiki/status.md`(모듈 진행상태·비고). ADR(`decisions/`)은 *왜*의 히스토리(현재 구속력 없음).
5. **보드 조회** — 지금 잡을 수 있는 ticket 확인:
   ```bash
   {{PY}} .project_manager/tools/board.py list
   ```
6. **직전 세션 핸드오프** — log 의 마지막 entry 만 (full Read 금지):
   ```bash
   {{PY}} .project_manager/tools/pm_log.py tail
   ```

> **부트스트랩 self-sufficient dump.** `pm_bootstrap.py` 는 board·git·회귀에 더해 **차수(`PM N차`)·log
> 마지막 handoff entry 본문 전체·pm_state 남은작업/사용자발의** 를 한 번에 surface 한다(ADR-0035) —
> `{{PY}} .project_manager/tools/pm_bootstrap.py`. board.py·pm_log.py 직접 호출은 baseline 재확인 시 보조 경로.

### 세션 식별

- **task 일반 사용자 경로** — 시작/재개는 `/pm-bootstrap --task <이름>`, 종료는
  `/pm-handoff --task <이름>`만 쓴다. 신규 task는 작업공간 0개로 시작해 PM이 후속 대여하고,
  **task 진입 시점에** pm_state를 즉시 만든다. 슬롯 대여는 그 뒤의 별도 작업이다. 기존 task는
  보유 슬롯 집합과 task pm_state를 자동 수령한다. Python backbone도
  `pm_bootstrap.py --task <이름>`·`pm_handoff.py --task <이름>`만 사용하며 task와
  repo/slot의 혼합 진입은 엔진이 거부한다.
- **PM 세션명 canonical = `<repo>_<N>`** (multi-PM 정체성 — `<repo>`=프로젝트 repo·`<N>`=PM 슬롯
  번호 · ADR-0043). board.py 조작 시 `--repo <repo> --slot <N>` 인자로 전달한다(ADR-0057 — 구 세션 플래그 대체):
  ```bash
  {{PY}} .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 예: --repo myproj --slot 1
  ```
  `--repo` 를 명시해야 board 가 repo prefix 를 유도한다.
  **솔로(M=1)** 는 `--repo/--slot` 을 생략해도 된다 — 아래 식별 우선순위 체인이 해소한다.
  (board.py 식별 우선순위·ADR-0040: `--repo`/`--slot` 인자 > `$PM_SESSION_NAME`[구 `$CLAUDE_SESSION_NAME` = deprecated alias] > **활성 슬롯 lease 가 정확히 1개면 그 세션**[단일-lease 유도] > [lease 장부 부재·leased 0 = 솔로] local.conf `session=` > 미해소[귀속 쓰기는 fail-loud·`--repo <repo> --slot <N>` 명시 요구]. leased ≥2[모호]면 local.conf 층을 건너뛴다 — 남의 세션 silent 오귀속 차단.)
- 위임의 식별 라벨 — `orch-dev-TNNNN` / `orch-review-TNNNN` (위임 규약은 하네스 네이티브 채널).

### 첫 turn 권장 보고 (부트스트랩 직후)

1. **board 1줄** — `done N / open N / claimed N / blocked N` + 회귀·lint·git 상태.
2. **직전 세션 요약 3~5줄** — CLI 가 dump 한 handoff entry 본문에서 핵심 산출물·메타 학습 *요약*.
3. **다음 옵션 N개** — CLI 가 surface 한 pm_state "남은 작업" + open ticket 기반.
4. **결정 요청** — *무엇부터 갈까요?* + 권장 시퀀스 1줄. (결정은 사용자.)

## 4. 작업 완료 부기 (PM 손)

ticket 을 닫을 때:

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
```

추가로 PM 이:
- `.project_manager/wiki/status.md` 해당 모듈 행 갱신.
- log 에 handoff entry append (핸드오프 시 `pm_handoff.py` 가 skeleton 생성).
- 회귀 `{{TEST_CMD}}` 통과 확인 (red 면 닫지 않는다).
- git commit — 논리적 체크포인트. **커밋할 경로를 명시**한다:
  ```bash
  git add <이번에 새로 만든 파일 경로들>          # 미추적 경로는 add 선행 필수
  git commit -m "T-NNNN — <요약>" -- \
    <ticket touches 의 실경로들> \
    .project_manager/wiki/status.md .project_manager/wiki/log/current.md \
    .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
    .project_manager/wiki/tickets/done/T-NNNN-<slug>.md
  ```
  pathspec 없는 bare commit 은 *남이 stage 해 둔* 무관한 변경까지 싣는다(공유 워킹트리 mutation 은
  선언된 경로만 · ADR-0074). ⚠️ 미추적(신규) 파일을 pathspec 에 바로 주면 `error: pathspec '…' did
  not match any file(s) known to git` 으로 커밋 전체가 rc=1 로 죽는다 — `git add` 를 먼저 하라.
  ⚠️ 티켓 파일이 `claimed/`→`done/` 으로 옮겨졌으면 **옛/새 경로를 함께** 줘야 그 이동이 실린다.
  메시지 말미 `Co-Authored-By` 트레일러.

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
<!-- TODO: 이 프로젝트의 아키텍처 불변식·안전 경계. 위임 프롬프트(reviewer)에도 인용된다.
     제약이 없으면 이 절을 통째로 삭제해도 된다. -->

## 7. 자주 쓰는 명령

엔진이 인코딩을 코드로 처리하므로 env prefix 없이 그대로 호출한다 (§1).

```bash
# 보드
{{PY}} .project_manager/tools/board.py list
{{PY}} .project_manager/tools/board.py show T-NNNN
{{PY}} .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 솔로(M=1)면 생략 가능 (§세션 식별)
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --tag phase-1
{{PY}} .project_manager/tools/board.py lint           # depends_on·thin-ticket 검사

# log
{{PY}} .project_manager/tools/pm_log.py tail          # 마지막 entry (의미단위 읽기)
{{PY}} .project_manager/tools/pm_log.py archive --before YYYY-MM-DD

# 핸드오프 (세션 종료)
{{PY}} .project_manager/tools/pm_handoff.py --dry-run
# task 사용자 경로
{{PY}} .project_manager/tools/pm_handoff.py --task <이름>

# 엔진 동기화 (메인테이너 · 루트 → 이 타깃)
{{PY}} .project_manager/tools/pm_update.py --from <upstream> --dry-run
```

> 위임(dev/reviewer/architect/researcher)은 각 하네스의 네이티브 채널로 한다 — 위임 규약(도구·
> 프롬프트·role 매핑)의 단일 진실은 하네스별 운영 지침이 담는다.

> **ctx 예산 (핸드오프 임계 분모 · ADR-0041):** ctx 정지/넛지 %의 100% 기준은 `.project_manager/local.conf`
> 의 `ctx_window_tokens_<harness>`(하네스별 키) > generic `ctx_window_tokens` > 200000 순으로 해소된다.
> 하네스별 키가 우선 — 한 repo 를 여러 하네스로 동시 운용하면 각자 예산을 독립 설정한다 (미설정 시 기본 200000).

## 8. 핵심 디렉토리

| 경로 / 구성요소 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py · pm_log.py (공유 엔진 · 0 수정) |
| `.project_manager/wiki/` | 비-코드 산출물 (작업/결정/사양/상태/**domain 지식 레이어**(§10)/pm_role·pm_state·pm_playbook/log/raw) |
| PM-workflow 스킬 | canonical `SKILL.md` **단일 소비**(ADR-0065) — 하네스가 네이티브 스캔·슬래시(`/pm-…`) 호출 (스킬 스캔 비활성화 금지). |
| 하네스 어댑터 (하네스별 디렉토리) | subagent 정의 + 하네스-고유 실행 모델·위임 채널 (경로·규약은 하네스 운영 지침). |
| `AGENTS.md` | 이 파일 — PM 부트스트랩·공통 코어 (하네스 공용 진입 doc). |

## 9. 막혔을 때

- 의존 ticket 미완 / 외부 키 없음 → `board.py block --reason "..."`.
- 잘못 claim → `board.py unclaim`.
- ticket 본문 부족 → 먼저 본문 보강하고 계속 (본문이 단일 진실).
- 모르는 결정 필요 → ADR 작성 후 진행 (`.project_manager/wiki/decisions/`).
- 위임이 깨지거나 결과가 불완전 → 재위임 전에 ticket 본문·컨텍스트 예산을 점검한다.

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
- ADR-0006 · ADR-0069 (`.project_manager/wiki/decisions/`) — 진입 doc 공통 코어 + 하네스별 전달 채널 결정
