# CLAUDE.md

> Claude Code 세션 시작 시 자동 로드되는 진입점. **새 세션이면 먼저 읽고 → `board.py list` 로 보드를 확인하라.**

## 프로젝트 한 줄

{{PROJECT_TAGLINE}}
<!-- TODO: {{PROJECT_NAME}} 가 무엇을 하는 시스템인지 1~2 문장. -->

## 새 세션 부트스트랩 (3 단계)

1. **상황 파악** — 순서대로 본다:
   - 보드 — `{{PY}} .project_manager/tools/board.py list`. `board.md` 는 파생 대시보드라 git 에 없을 수 있다. 파일로 보려면 `board.py refresh`.
   - [`.project_manager/wiki/architecture.md`](.project_manager/wiki/architecture.md) — **현재-아키텍처 단일 진실**(① live / ② target). 부트스트랩 1순위이며 충돌 시 기준.
   - [`.project_manager/wiki/status.md`](.project_manager/wiki/status.md) — 모듈 진행상태·비고.
   - [`.project_manager/wiki/domain/`](.project_manager/wiki/domain/) — architecture 세부 지식(concept · `covers:` 코드 링크 · freshness).
   결정 근거·히스토리는 [`.project_manager/wiki/decisions/`](.project_manager/wiki/decisions/). ADR 은 현재 구속력 없음(현재 기준은 architecture.md).
2. **세션 이름 정하기** — **PM 세션명 canonical = `<repo>_<N>`**(`<repo>`=프로젝트 repo, `<N>`=PM 슬롯 번호). 아래 `claim` 의 `--repo <repo> --slot <N>` 으로 전달하며 `--repo` 가 board 의 repo prefix 를 유도한다. **솔로(M=1)** 는 `--repo/--slot` 생략 가능. CLI 에서는 `export PM_SESSION_NAME=<repo>_<N>` 도 가능. 식별 우선순위: `--repo`/`--slot` > `$PM_SESSION_NAME`(구 `$CLAUDE_SESSION_NAME` deprecated alias도 인식) > **활성 슬롯 lease 가 정확히 1개면 그 세션**(단일-lease 유도) > (lease 장부 부재·leased 0인 솔로) `local.conf session=` legacy 폴백 > 미해소. 미해소 시 귀속 쓰기(claim 등)는 fail-loud하며 `--repo <repo> --slot <N>` 을 요구한다. **leased ≥2면 `local.conf session=` 층을 건너뛴다.**
3. **Ticket 잡기** — 외부 의존이 없고 다른 세션이 claim 하지 않은 것을 고른다:
   ```bash
   {{PY}} .project_manager/tools/board.py list --status open
   {{PY}} .project_manager/tools/board.py show T-NNNN
   {{PY}} .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 예: --repo myproj --slot 1 · 솔로(M=1)면 생략 가능
   ```
   ticket 본문의 **목표 / 인터페이스 / 완료 조건 / 참고 링크** 만으로 작업 가능해야 한다. 부족하면 본문부터 보강한다.

## 멀티-PM clone (동시 다중 PM 프로젝트)

여러 clone 에서 영역을 나눠 PM 하면 **clone 당 1회**:

```bash
{{PY}} .project_manager/tools/board.py init --prefix pay --area "결제" --owner alice
```

- `areas.md`(공유 레지스트리) prefix 등록 + `local.conf`(per-clone·git-ignored) 생성 + `pm_state.md` 로컬 생성.
- 이후 `board.py new` 는 영역별 네임스페이스 `T-pay-NNN` 으로 발행한다.
- **3계층:** 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf).
- **솔로:** `board.py init`(prefix 없이) → pm_state·pre-push 회귀 훅·legacy `T-NNNN` setup, areas.md 미생성. init 없이도 `board.py new` 는 동작한다.

## 작업이 끝나면

```bash
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass     # 또는
{{PY}} .project_manager/tools/board.py block T-NNNN --reason "..."
```

추가 작업:
- `.project_manager/wiki/status.md` 해당 모듈 행 갱신
- `.project_manager/wiki/log/current.md` 에 entry append
- 회귀 테스트 `{{TEST_CMD}}` 통과 확인
- **git commit** — 논리적 체크포인트에서 **커밋 경로를 명시**:
  ```bash
  git add <이번에 새로 만든 파일 경로들>          # 미추적 경로는 add 선행 필수 (아래 주의)
  git commit -m "T-NNNN — <요약>" -- \
    <ticket touches 의 실경로들> \
    .project_manager/wiki/status.md .project_manager/wiki/log/current.md \
    .project_manager/wiki/tickets/claimed/T-NNNN-<slug>.md \
    .project_manager/wiki/tickets/done/T-NNNN-<slug>.md
  ```
  pathspec 없는 bare commit 은 남이 stage 한 무관한 변경도 싣는다. ⚠️ **미추적(신규) 파일은 `git add` 선행** — pathspec 에 바로 주면 `error: pathspec '…' did not match any file(s) known to git` 으로 전체 commit 이 rc=1. ⚠️ ticket 이 `claimed/`→`done/` 으로 이동했으면 **옛/새 경로를 함께** 줘야 이동이 커밋된다. 메시지 말미에 `Co-Authored-By: Claude` 트레일러. 시크릿은 `.gitignore` 로 영구 제외.

> **참조 규약**: status·log·ticket·ADR 의 ADR/ticket/idea 참조는 ID-wikilink(`[[ADR-NNNN]]`·`[[T-NNNN]]`·`[[idea-NNNN]]`)만 사용. 생파일명·슬러그 금지(`board.py lint --gate` 강제). 단일 진실은 pm_playbook §참조 규약.

## 작업 원칙 (반드시)

- **작은 단위 분할 → 단계별 테스트 검증.** 한 모듈 = 한 ticket = 한 단계.
- **테스트 없이는 구현이 끝난 게 아니다.** 회귀 통과가 완료 조건.
- **최소 변경.** ticket 요구만 수행하고 무관한 리포맷·기능 추가 금지.
- **약어보다 명시적 풀네임**을 사용한다.
- **`.project_manager/` 는 숨김 디렉토리** — `ls -a` 또는 절대 경로로 접근.

### 프로젝트 고유 제약 (절대 위반 금지)

{{PROJECT_CONSTRAINTS}}
<!-- TODO: 이 프로젝트의 아키텍처 불변식·안전 경계를 적는다. 서브에이전트와
     모든 세션이 상속하는 절대 규칙. 예시 (도메인 무관):
       - 핵심 결정 로직(순수·결정론) ↔ 분석/생성 계층(LLM 등 비결정) 경계 엄격 — 섞지 않는다.
       - LLM·외부 호출 래퍼는 fail-soft — 예외를 raise 하지 않고 에러로 감싼다.
       - 외부 입력은 sanitize 후에만 핵심 로직/LLM 에 전달.
     제약이 없으면 이 절을 통째로 삭제해도 된다. -->

## Windows / 인코딩 (CP949 캐비엇)

- 엔진 wiki·ticket 은 UTF-8. 파일 IO 는 `encoding="utf-8"`, 콘솔은 `sys.stdout/stderr.reconfigure(encoding="utf-8")` 로 처리하므로 **Windows/CP949·PowerShell 에서도 env prefix 없이** board·log 한글이 동작한다. 외부 파이프·서드파티 도구·구버전 콘솔에서 필요하면 셸별로 PowerShell `$env:PYTHONUTF8='1';`, bash `PYTHONUTF8=1` 을 붙인다.
- **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError) — `cd X && cmd` 금지. 도구 workdir 파라미터나 명령 분리로 실행한다. Windows 루트 facade 는 `.\pm-config.cmd`·`.\pm-update.cmd`(bash 불요), `./pm-*.sh` 는 bash 용.
- `{{PY}}` 는 `board.py init` 이 PATH 탐지로 채운다(Windows=`python`·POSIX=`python3`). venv 이면 PM workflow 도구가 `venv/Scripts/python.exe`(Windows)·`venv/bin/python`(POSIX)을 자동 선택하고, 없으면 현재 인터프리터로 폴백한다.

## 자주 쓰는 명령

```bash
# 전체 테스트 (테스트 수 단일 진실 = pytest 실측 · status.md 는 숫자 미저장·judgment-only·ADR-0023)
{{TEST_CMD}}

# 보드 조작
{{PY}} .project_manager/tools/board.py list
{{PY}} .project_manager/tools/board.py show T-NNNN
{{PY}} .project_manager/tools/board.py claim T-NNNN --repo <repo> --slot <N>   # 솔로(M=1)면 생략 가능 (§세션 이름)
{{PY}} .project_manager/tools/board.py complete T-NNNN --tests-pass
{{PY}} .project_manager/tools/board.py new "title" --touches a.py,b.py --tag phase-1
{{PY}} .project_manager/tools/board.py lint     # 의존성·thin-ticket 일관성 검사

# 프레임워크 갱신 (메인테이너·업그레이드당 1회 — 절차는 pm_playbook §"프레임워크 갱신"):
{{PY}} .project_manager/tools/pm_update.py --from <upstream-checkout> --dry-run   # 엔진만 당겨옴 → --dry-run 빼고 적용 → 검증 → 커밋·push

# PM workflow 도구(pm_bootstrap·pm_handoff·ticket_finish)는 PM 세션 전용 — 용법·플래그·호출(/pm-*)의
# 단일 진실은 pm_role.md §"skill 카탈로그" + 각 .claude/skills/pm-*/SKILL.md. 여기에 재나열하지 않는다.
# log 관리 도구만 여기 (전용 skill 없음):
{{PY}} .project_manager/tools/pm_log.py tail                       # 마지막 entry 만 (의미단위 읽기)
{{PY}} .project_manager/tools/pm_log.py archive --before YYYY-MM-DD  # 그 이전 entry 를 log/archive/ 봉인
{{PY}} .project_manager/tools/pm_log.py migrate                    # 기존 log.md → archive/0000-legacy (도입 1회)

# domain 지식 레이어 (살아있는 프로젝트 지식 — 명령은 아래·살아있는 루프 전체는 프레임워크 가이드 · ADR-0018)
{{PY}} .project_manager/tools/domain.py list                       # 페이지 카탈로그 (type·covers·stale)
{{PY}} .project_manager/tools/domain.py affected --ticket T-NNNN   # ticket touches∩covers 페이지 소환
{{PY}} .project_manager/tools/domain.py capture --tickets T-NNNN   # 갱신 reminder (채록)
{{PY}} .project_manager/tools/domain.py lint                       # freshness — stale 페이지 검사
```

> **ctx 예산:** ctx 정지/넛지 %의 100% 기준은 `.project_manager/local.conf` 의 `ctx_window_tokens_<harness>`(예 `ctx_window_tokens_claude=500000`) > generic `ctx_window_tokens` > 200000 순. 한 repo 를 여러 harness 로 동시 운용하면 각각 설정한다.

## 핵심 디렉토리

| 경로 | 의미 |
|---|---|
| `.project_manager/tools/` | board.py · ticket_finish.py · pm_bootstrap.py · pm_handoff.py · pm_log.py (숨김 — `ls -a`) |
| `.project_manager/wiki/` | 비-코드 산출물(작업·결정·사양·상태·domain·pm_role·pm_state·pm_playbook·log·raw) |
| `.claude/agents/` | researcher · architect(Opus) · developer · code-reviewer 서브에이전트 정의 |
| `.claude/skills/` | PM workflow slash command skill(단일 진실: pm_role.md §skill 카탈로그) |
<!-- TODO: 프로젝트의 실제 코드 디렉토리 행을 여기 추가한다. -->

## 막혔을 때

- 의존 ticket 미완 또는 외부 키 없음 → `board.py block --reason "..."`.
- 잘못 claim → `board.py unclaim`.
- ticket 본문 부족 → 본문부터 보강.
- 모르는 결정 필요 → [`.project_manager/wiki/decisions/README.md`](.project_manager/wiki/decisions/README.md) 절차로 ADR 작성 후 진행.
