# AGENTS.md — PM 어댑터 공통 코어

> 세션 진입점의 **harness-neutral 공통 코어**. 프로젝트 정체성·엔진 호출·완료 부기·결정 권한·안전 가드를 공유하며, 하네스별 실행·위임 지침과 함께 적용한다.

## 프로젝트 한 줄

한 줄 프로젝트 설명
<!-- TODO: project_manager 가 무엇을 하는 시스템인지 1~2 문장. -->

## 1. 엔진 호출 규약 (인코딩)

env prefix 없이 호출한다:

```bash
python3 .project_manager/tools/board.py list
```

- 엔진이 파일 IO `encoding="utf-8"`·콘솔 stdout reconfigure를 처리하므로 Windows/CP949·PowerShell에서도 env prefix가 불필요하다.
- 구버전 Windows·서드파티 파이프에서 필요할 때만 셸 문법으로 붙인다: PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1`. bash 문법을 강제하지 않는다.
- **PowerShell 5.x는 `&&` 체이닝 미지원**(ParseError)이다. `cd X && cmd` 금지; 도구 workdir 파라미터나 명령 분리로 실행한다. Windows 루트 facade는 `.\pm-config.cmd`·`.\pm-update.cmd`(bash 불요).

`python3`는 setup 시 채택 환경 인터프리터로 치환된다. `.project_manager/local.conf`의 `py=`가 단일 진실이며 `board.py init`이 설정한다(venv면 `venv/bin/python`).

## 2. PM 부트스트랩 (세션 시작 시 순서)

**Read tool로 파일을 읽을 땐 절대 경로를 쓴다.**

<!-- pm-bootstrap-preread:start -->
세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, `$pm-bootstrap` dump 한 번뿐이다.

1. **이 문서(AGENTS.md·공통 코어)** — 이미 로드된 프로젝트 규칙·형상.
2. **현재 정체성의 `pm_state`** — task는 `.project_manager/.local/tasks/<task>/pm_state.md`,
   slot은 `.project_manager/.local/slots/<repo>_<N>/pm_state.md`, 솔로는 `wiki/pm_state.md`
   legacy 폴백. 신규 task는 bootstrap 진입 전 파일이 없어도 정상이다.
3. **`$pm-bootstrap` dump 한 번** — board·git·차수·직전 handoff 본문·남은 작업을 한꺼번에
   surface한다. Python backbone은 `python3 .project_manager/tools/pm_bootstrap.py`다.
<!-- pm-bootstrap-preread:end -->

정식 계약은 `.project_manager/wiki/pm_role.md` §부트스트랩이 단일 진실이다.
`architecture.md`·`status.md`·`decisions/`·`roadmap.md`·전체 보드·타 슬롯 log는 시작 시
통독하지 않고 실제 필요가 생길 때 해당 절만 읽는다.

**현재 진실:** `architecture.md`는 현재 아키텍처 단일 진실이며, 옛 ADR 또는 현재 의도·실측과
충돌하면 기준으로 따른다. 바뀐 것은 읽는 시점뿐이다.

### 세션 식별

- **task 사용자 경로** — 시작/재개는 `$pm-bootstrap --task <이름>`, 종료는 `$pm-handoff --task <이름>`만 쓴다. 이 표기는 스킬 진입 표기이며 자체 slash command를 뜻하지 않는다. 신규 task는 작업공간 0개로 시작하고 task 진입 시 pm_state를 즉시 만든 뒤 PM이 슬롯을 별도로 대여한다. 기존 task는 보유 슬롯 집합과 task pm_state를 수령한다. Python도 `pm_bootstrap.py --task <이름>`·`pm_handoff.py --task <이름>`만 사용하며 task와 repo/slot 혼합 진입은 엔진이 거부한다.
- **PM 세션명 canonical=`<repo>_<N>`**(`<repo>`=프로젝트 repo, `<N>`=PM 슬롯). board 쓰기는 `--repo <repo> --slot <N>`을 전달한다:
  ```bash
  python3 .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 예: --repo myproj --slot 1
  ```
  `--repo`가 repo prefix를 유도한다. **솔로(M=1)**는 `--repo/--slot` 생략 가능하다. 식별 우선순위는 `--repo`/`--slot` > `$PM_SESSION_NAME`(구 `$CLAUDE_SESSION_NAME` deprecated alias) > 활성 슬롯 lease가 정확히 1개면 그 세션(단일-lease 유도) > lease 장부 부재·leased 0이면 local.conf `session=` > 미해소면 귀속 쓰기 fail-loud 및 `--repo <repo> --slot <N>` 요구. leased ≥2면 local.conf 층을 건너뛰어 silent 오귀속을 막는다.
- 위임 식별 라벨: `orch-dev-TNNNN` / `orch-review-TNNNN`(위임 규약은 하네스 네이티브 채널).

### 첫 turn 권장 보고 (부트스트랩 직후)

1. board 1줄: `done N / open N / claimed N / blocked N` + 회귀·lint·git 상태.
2. CLI가 dump한 handoff에서 핵심 산출물·메타 학습 3~5줄 요약.
3. pm_state "남은 작업" + open ticket 기반 다음 옵션.
4. *무엇부터 갈까요?* 결정 요청 + 권장 시퀀스 1줄. 결정은 사용자 몫이다.

## 4. 작업 완료 부기 (PM 손)

ticket을 닫을 때:

```bash
python3 .project_manager/tools/board.py complete T-NNNN --tests-pass
```

추가로 PM이:

- `.project_manager/wiki/status.md` 해당 모듈 행 갱신.
- log에 handoff entry append(`pm_handoff.py`가 skeleton 생성).
- 회귀 `python3 -m pytest tests/ -q` 통과 확인(red면 닫지 않는다).
- 논리적 체크포인트를 커밋하되 경로를 명시:
  ```bash
  git add <이번에 새로 만든 파일 경로들>          # 미추적 경로는 add 선행 필수
  git commit -m "T-NNNN — <요약>" -- \
    <ticket touches 의 실경로들> \
    .project_manager/wiki/status.md .project_manager/wiki/log/current.md \
    .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
    .project_manager/wiki/tickets/done/T-NNNN-<slug>.md
  ```
  bare commit은 남이 stage한 변경까지 싣는다. 공유 워킹트리는 선언 경로만 커밋한다. ⚠️ 미추적 파일을 pathspec에 바로 주면 `error: pathspec '…' did not match any file(s) known to git`로 전체 커밋이 rc=1이므로 먼저 `git add`한다. ⚠️ `claimed/`→`done/` 이동은 옛/새 경로를 함께 지정한다. 메시지 말미에 `Co-Authored-By` 트레일러를 둔다.

ADR/ticket/idea 참조는 ID-wikilink(`[[ADR-NNNN]]`·`[[T-NNNN]]`·`[[idea-NNNN]]`)만 사용한다. 생파일명·슬러그는 금지되며 `board.py lint --gate`가 강제한다. 단일 진실은 [[pm_playbook]] §참조 규약이다.

## 5. PM 결정 권한

PM은 *어떻게*를 자율 결정하고, 사용자는 *무엇을·얼마의 비용으로·밖으로 내보낼지* 결정한다.

- **자율 + 사후 로그** — 새 ticket 발행/super-ticket 분할/depends_on 변경/block·unblock/spec 추출/일상 ADR(`scope: internal-process`)/위임.
- **사용자 게이트(사전 동의)** — 미션·핵심 안전 경계/유료·한도 API 대량 호출/키 발급·외부 게시·배포/`scope: mission` ADR. 상세: [[pm_role.local.md]] §사용자 게이트.
- **금지(PM·사용자 단독 불가)** — 미션 변경/핵심 안전 경계 약화/영구 수동 영역 자동화. 양측 합의+별도 ADR 필요. 상세: [[pm_role.local.md]] §금지.

## 6. 라이브 외부 행위 안전 가드

- 단위 테스트는 모두 mock; 라이브 외부 API 호출은 통합 테스트 마커로만 한다.
- 외부 비가역 행위(네트워크 송신·배포·키 발급)가 가능한 ticket은 사용자 명시 승인 후 진행한다.
- **프로덕션 진입점을 라이브로 실행하지 않는다.** mock 격리 자동 테스트로만 검증한다.
- 새 외부 비가역 행위에는 테스트 중 거부·opt-in 환경변수의 코드 안전 가드를 둔다.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}} <!-- TODO: 손으로 채우세요 -->
<!-- TODO: 이 프로젝트의 아키텍처 불변식·안전 경계. 위임 프롬프트(reviewer)에도 인용된다.
     제약이 없으면 이 절을 통째로 삭제해도 된다. -->

## 7. 자주 쓰는 명령

엔진이 인코딩을 처리하므로 env prefix 없이 호출한다(§1).

```bash
# 보드
python3 .project_manager/tools/board.py list
python3 .project_manager/tools/board.py show T-NNNN
python3 .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 솔로(M=1)면 생략 가능 (§세션 식별)
python3 .project_manager/tools/board.py complete T-NNNN --tests-pass
python3 .project_manager/tools/board.py new "title" --touches a.py,b.py --tag phase-1
python3 .project_manager/tools/board.py lint           # depends_on·thin-ticket 검사

# log
python3 .project_manager/tools/pm_log.py tail          # 마지막 entry (의미단위 읽기)
python3 .project_manager/tools/pm_log.py archive --before YYYY-MM-DD

# 핸드오프 (세션 종료)
python3 .project_manager/tools/pm_handoff.py --dry-run
# task 사용자 경로
python3 .project_manager/tools/pm_handoff.py --task <이름>

# 엔진 동기화 (메인테이너 · 루트 → 이 타깃)
python3 .project_manager/tools/pm_update.py --from <upstream> --dry-run
```

위임(dev/reviewer/architect/researcher)은 하네스 네이티브 채널로 한다. 도구·프롬프트·role 매핑은 하네스별 운영 지침이 단일 진실이다.

ctx 정지 밴드(현행 의미 = 최종 checkpoint 넛지·키 이름은 호환 유지)/넛지 %의 100% 기준은 `.project_manager/local.conf`의 `ctx_window_tokens_<harness>` > generic `ctx_window_tokens` > 200000 순으로 해소한다. 여러 하네스를 함께 쓰면 하네스별 키로 독립 설정하며, 미설정 기본은 200000이다.

## 8. 핵심 디렉토리

| 경로 / 구성요소 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py · pm_log.py(공유 엔진 · 0 수정) |
| `.project_manager/wiki/` | 비-코드 산출물(작업/결정/사양/상태/domain 지식 레이어(§10)/pm_role·pm_state·pm_playbook/log/raw) |
| PM-workflow 스킬 | canonical `SKILL.md` 단일 소비. `$pm-bootstrap` 같은 진입 표기로 호출하며 스킬 스캔 비활성화 금지. |
| 하네스 어댑터(하네스별 디렉토리) | subagent 정의 + 하네스별 실행 모델·위임 채널 |
| `AGENTS.md` | 이 파일: PM 부트스트랩·공통 코어 |

## 9. 막혔을 때

- 의존 ticket 미완/외부 키 없음 → `board.py block --reason "..."`.
- 잘못 claim → `board.py unclaim`.
- ticket 본문 부족 → 단일 진실인 본문을 먼저 보강.
- 모르는 결정 필요 → `.project_manager/wiki/decisions/`에 ADR 작성 후 진행.
- 위임 실패/결과 불완전 → 재위임 전에 ticket 본문·컨텍스트 예산 점검.

## 10. domain 지식 레이어 (살아있는 프로젝트 지식)

`.project_manager/wiki/domain/`은 현재 프로젝트의 무엇·어떻게를 계속 갱신하는 지식 그래프다. `architecture.md`가 구조·모듈·구현상태의 현재 단일 진실이고, domain 페이지는 `covers:` 코드 글롭 단위 세부 개념·절차·조사를 다룬다. architecture↔domain 충돌은 의도↔현실 드리프트로 취급한다.

`domain/_template.md`를 `domain/<주제>.md`로 복사해 작성한다:

- `type:` concept(무엇·왜) | guide(어떻게·절차) | research(조사·누적)
- `covers:` 담당 코드 글롭(예 `src/foo/**`), 코드 무관이면 비움.
- `derived:` false(사람 author) | true(코드 자동생성·손대지 마)

한 페이지에는 한 가지만 두고 `[[다른-페이지]]`로 연결한다(wikilink lint 검증).

```bash
python3 .project_manager/tools/domain.py list                      # 페이지 카탈로그 (type·covers·stale)
python3 .project_manager/tools/domain.py affected --ticket T-NNNN  # ticket touches 와 겹치는 covers 페이지 (소환)
python3 .project_manager/tools/domain.py capture --tickets T-NNNN  # touch∩covers 갱신 reminder (채록)
python3 .project_manager/tools/domain.py lint                      # freshness — stale 페이지 검사
```

코드 touch → `domain affected`로 겹치는 페이지 소환 → `domain capture` reminder → 채록한다. `covers` 코드가 페이지 `updated` 후 바뀌면 `domain lint`가 **stale** ⚠로 가시화하되 막지는 않는다. PM은 위임 전에 영향 페이지를 dev 프롬프트에 동반하고 완료 때 채록한다.

## 참고

- `.project_manager/wiki/pm_role.md` — PM 책임·결정 권한·핸드오프 단일 진실
- `.project_manager/wiki/pm_playbook.md` — Wave 패턴·메타 정책(필요 시 Read)
- ADR-0006 · ADR-0069 (`.project_manager/wiki/decisions/`) — 진입 doc 공통 코어 + 하네스별 전달 채널 결정
